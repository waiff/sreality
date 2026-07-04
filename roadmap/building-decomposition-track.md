> Track file — part of [ROADMAP.md](../ROADMAP.md). After shipping, edit only this file + its index row.

## Building decomposition track (parallel)

The "paste a whole-building listing" workflow. Operator drops a
`rodinný dům` URL into the same paste field they use for apartments
today; the system reads description + floor-plan images, proposes
the apartment units inside the building (including potential ones
like an unconverted attic), the operator confirms / edits the unit
list and the per-unit condition, the agent fans out one rent + one
sale estimate per unit, results are grouped and summed at the
building level, and a spreadsheet-style business-case overlay
computes the development P&L (acquisition + reno + new build + soft
costs + VAT in/out + debt service → EBIT / EBT / MOIC / IRR /
yield-on-cost).

Reference business case: `model_Kralupska.xlsx` (operator-supplied,
2026-05-12). Six blocks: Assumptions, Floor Schedule, Unit
Schedule, Cost Stack (with VAT splits), Revenue & P&L, Returns.

### Phase B0: Schema + scaffolding (done)

Pure plumbing. No agent changes, no UI changes beyond type stubs.
Shipped in PR #59.
- Migration 035 (`035_building_runs.sql`): new `building_runs`
  parent table; `building_run_id` (FK) + `building_unit_id` (text)
  columns on `estimation_runs`. Status lifecycle: `pending` →
  `extracting` → `awaiting_input` → `estimating` → `success` |
  `failed`. The `awaiting_input` pause is the human-in-the-loop gate
  that distinguishes the building flow from today's single-shot
  estimation_runs flow. Per CLAUDE.md architectural rule #13.
- `api/building_runs.py` module: `create_building_run`,
  `get_building_run`, `list_building_runs`. Children are surfaced
  on the detail response via a side-query on `estimation_runs`.
- API endpoints: `POST /buildings` (minimal shell — `{source,
  input_url?}` → `status='pending'`), `GET /buildings`,
  `GET /buildings/{id}`. All bearer-gated.
- Pydantic schemas: `CreateBuildingIn`, `BuildingUnit` (the JSONB
  unit record schema, used by B1 onwards), `BuildingRunOut` shape
  documented via `_BUILDING_COLUMNS`.
- Frontend type stubs in `frontend/src/lib/types.ts` (`BuildingRun`,
  `BuildingUnit`, `BuildingStatus`). No new pages or components.
- Tests: hermetic CRUD tests in `tests/api/test_buildings.py`
  modeled on the `_State`-style fakes from `test_estimations.py`.

### Phase B1: URL ingest + unit extractor + confirmation UI (done)

Builds on B0's persistence. The output of B1 is a `building_runs`
row sitting in `status='awaiting_input'` (extractor ran,
`units_proposal` populated, ready for the operator's review) which
transitions to `estimating` on confirmation. Per-unit fan-out
lands in B2; B1 stops at the human-in-the-loop gate.

**Data + migration**

- Migration 036 (`036_building_unit_extractions.sql`):
  - New cache table `building_unit_extractions` keyed on
    `(sreality_id, snapshot_id)` — same shape as `listing_summaries`
    (migration 027): `extracted_at`, `model`, `units jsonb`,
    `building jsonb`, `confidence text`, `warnings jsonb`,
    `cost_usd numeric`. New snapshot auto-invalidates by virtue of
    the PK including `snapshot_id`. RLS enabled, no policies (read
    through API).
  - `'extract_building_units'` added to `llm_calls.called_for`
    CHECK constraint so the audit trail tags vision calls
    consistently.
  - Four `app_settings` seeds:
    `llm_building_extractor_system_prompt` (the canonical prompt
    body, mirrors the on-disk SKILL.md),
    `llm_building_extractor_model` (`claude-sonnet-4-5` by default,
    operator-tunable via `/settings`),
    `llm_building_extractor_max_images` (default `8` — enough to
    cover hero + floor plans + interior on a typical sreality `dum`
    listing without ballooning the token bill), and
    `building_default_estimator_skill` (default
    `rental_estimator_v1`, used by the B2 orchestrator — see the
    "apartment skill reuse" note on B2's orchestrator step).
  - Every prior value preserved via the existing
    `app_settings_history` trigger (migration 020).

**Toolkit function**

- `toolkit.building_extraction.extract_building_units(
  sreality_id, snapshot_id, max_images=8, force_refresh=False) ->
  envelope`. Write-allowed exception per CLAUDE.md toolkit rule #5
  (LLM is the source of truth; cache locally so the
  inevitable B1→B2 round-trip and any later re-extraction don't
  re-bill). Same envelope contract as every other toolkit function
  (`{data, metadata}`). The `data` payload is the structured unit
  proposal:
  ```python
  {
    "units": [
      {"id": "u1", "floor": 1, "area_m2": 72, "disposition": "3+kk",
       "condition": "good", "notes": "...", "is_potential": false},
      ...
    ],
    "building": {"floor_count": 4, "year_built": 1932,
                 "condition": "good", "total_area_m2": 320,
                 "construction_type": "brick"},
    "confidence": "high|medium|low",
    "warnings": [...],
  }
  ```
- Pulls description text from the latest snapshot's parsed fields
  (already on `listing_snapshots.raw_json`) and up to `max_images`
  images from R2 via boto3 `GetObject`, base64-encoded into the
  Claude vision payload — same pattern as `compare_listing_images`
  in `toolkit.image_similarity`.
- Calls log to `llm_calls` with
  `called_for='extract_building_units'`, the building_run_id (when
  invoked through the API), token / cost columns populated.
- Cohort floor: if the listing has no images in R2 (image-download
  phase hasn't caught up yet, or `R2_*` env vars missing), the
  function falls back to description-only and stamps
  `confidence='low'` + a warning. Never crashes the building flow.

**Skill — and why it is NOT the apartment estimator**

- New skill `building_unit_extractor_v1`:
  - On-disk seed: `skills/building_unit_extractor_v1/SKILL.md`
    (canonical content + frontmatter, mirroring
    `skills/rental_estimator_v1/SKILL.md`).
  - Migration 036 seed `INSERT` into `skills` table (same pattern
    as migration 029's `rental_estimator_v1` seed, migration 032's
    `rental_estimator_full_v1` seed). Operator edits live values
    via `/settings`; `skills_history` trigger preserves every
    prior version (per Phase 7 slice 1).
  - Allowed tools: `extract_building_units` (the toolkit wrapper
    above) + `record_building_units` (the terminator — same shape
    contract as `record_estimate`, validated server-side).
  - Preferred model: anthropic = `claude-sonnet-4-5`, gemini =
    `gemini-2.5-pro` (vision-capable on both providers).
  - Limits: `max_iterations: 4`, `max_cost_usd: 0.30`,
    `wall_clock_timeout_s: 90`. Lower than the estimator's caps
    because extraction is a one-shot vision call, not an iterative
    cohort search.
  - System prompt teaches the model to: (a) read the description
    text first to anchor on stated unit count + total area,
    (b) cross-check against floor plans, (c) emit one entry per
    discrete unit including potential ones (e.g. an unconverted
    attic worth flagging `is_potential=true`), (d) populate
    `condition` from the provided photos when the text is silent,
    (e) terminate with `record_building_units`.
  - **This is an extractor skill, not an estimator skill.** Per-unit
    rent / sale estimation in B2 reuses the existing
    `rental_estimator_v1` / `rental_estimator_full_v1` skill (see
    the apartment-skill-reuse note on B2's orchestrator step) so
    that an apartment estimated inside a building is computed
    exactly the same way as a standalone apartment estimation, and
    any improvement to the apartment estimator skill rolls into the
    building flow automatically.

**API endpoints**

- `POST /buildings/from_url` — operator-facing entry, replaces
  B0's minimal `POST /buildings` shell:
  1. Routes the input URL through
     `scraper.source_dispatcher.parse_listing_url` (reused as-is —
     same cache, same per-source parsers, same audit trail in
     `parsed_url_cache` and `llm_calls`).
  2. Validates the parse: `category_main` must be `'dum'` or
     `'komercni'`. A `byt` URL returns HTTP 400 with a hint to use
     `/estimations` instead — apartments don't decompose.
  3. Inserts `building_runs` row in `status='pending'` with all
     `input_*` + `source_*` + `subject_summary` columns populated
     from the parse output. (The `subject_summary.building` sub-
     object will be overwritten by the extractor's `building`
     field in step 5.)
  4. Transitions `status` to `'extracting'` and runs the
     extractor synchronously (v1; Phase 7 slice 2's async lifecycle
     will retrofit polling later). On extractor failure, transitions
     to `status='failed'` with `error_message` set; the row IS
     the audit trail, same discipline as estimation_runs.
  5. On success, writes the extractor output to `units_proposal`
     (append-only after this point) and to `subject_summary` (which
     keeps the operator-visible "what we know about the building"
     blob in one place), transitions to `status='awaiting_input'`,
     returns the row. Total latency ~10-30 s on a typical
     `dum` listing — within the 90s skill timeout.
- `POST /buildings/{id}/confirm_units` — the human-in-the-loop gate:
  1. Accepts the operator-edited unit list (the
     `record_building_units` envelope's `units` array). Rejects if
     `status != 'awaiting_input'` (HTTP 409 — building already in
     a later state).
  2. Validates each entry's shape via the existing `BuildingUnit`
     Pydantic schema from B0 (`id`, `floor`, `area_m2`,
     `disposition`, `condition`, `notes`, `is_potential`).
  3. Writes the confirmed list to `units` (mutable until estimation
     starts in B2, after which B2 freezes it).
  4. Transitions `status` to `'estimating'`. B2's orchestrator
     picks up from there. For B1's scope we stop here — a building
     in `estimating` with no child runs is a valid intermediate
     state.
- `POST /buildings/{id}/re_extract` — re-run the extractor against
  the current snapshot (forces cache miss via `force_refresh=True`).
  Only valid while the building is in `awaiting_input`; returns 409
  otherwise. Useful when a new snapshot lands between paste and
  confirmation and the operator wants the extractor to see it.
- B0's old minimal `POST /buildings` is removed — every operator-
  facing creation goes through `from_url` from B1 onward.

**Frontend**

- `NewEstimationModal` grows a `kind` toggle ("Apartment" /
  "Building"), defaulting to apartment so existing flows stay
  unchanged. Pasting a URL with `kind='building'` routes the
  request to `/buildings/from_url` instead of `/estimations`.
- Step 2 of the building flow renders a new `BuildingUnitEditor`
  component: a table of unit rows (floor / area / disposition /
  condition / notes / `is_potential` checkbox), add / remove
  buttons, plus a building summary header (year built, floor count,
  total m², construction type). Each editable field maps 1:1 to a
  `BuildingUnit` field. Submitting POSTs to
  `/buildings/{id}/confirm_units`.
- New `/building/:id` route — initial read-only view of a building
  row. For B1 it renders: subject summary block, current status
  badge (with a CTA for `awaiting_input` rows that opens the
  `BuildingUnitEditor` in confirm mode), units list (proposal or
  confirmed), warnings block, link back to the source URL. The
  full rollup view + per-unit estimate strips ship with B2.
- The Estimations list page (`/estimations`) is unchanged — building
  rows live on `/buildings` (a new list page) so the two
  conceptually-different things don't blend. `/buildings` is a slim
  table modeled on `/estimations` (source / status / created_at /
  unit count / link). The shared `EstimationsListPage` filter +
  pagination conventions apply.

**Tests**

- `tests/toolkit/test_building_extraction.py`: hermetic test that
  stubs the Claude vision call with a saved fixture response and
  exercises the cache hit / miss branches, plus the fallback path
  when R2 is unreachable.
- `tests/api/test_buildings_b1.py`: integration tests for
  `POST /buildings/from_url` (parse success, `byt` rejection,
  extractor failure, cache hit) and
  `POST /buildings/{id}/confirm_units` (happy path, status guard,
  schema validation). Modeled on the `_State` fakes from
  `tests/api/test_estimations.py`; no real LLM, no real DB.
- `tests/skills/test_building_unit_extractor_v1.py`: validates the
  SKILL.md frontmatter + migration seed are in sync (same pattern
  as the existing `rental_estimator_v1` test).
- Frontend: `BuildingUnitEditor.test.tsx` snapshot + interaction
  test for add / remove / edit / submit; `BuildingPage.test.tsx`
  for the `awaiting_input` CTA branch.

**Out of scope for B1 (deferred to B2 / later)**

- Per-unit rent / sale estimation fan-out — that's the B2
  orchestrator's job, which reuses the existing apartment
  estimator skill (see B2 below).
- Building rollup totals — same.
- The Excel-style business case tab — B3.
- Async / polling lifecycle — Phase 7 slice 2.
- Multi-portal (bezrealitky / idnes / remax) building paste — the
  source_dispatcher already routes those URLs, but per-source
  building parsers may need extra fields beyond what the apartment
  flow exercises; defer until a real bezrealitky `dum` URL surfaces
  in operator testing.

### Phase B2: Per-unit fan-out + building rollup view (done)

Takes the flow from B1's confirmation gate through to per-unit
estimates + a building-level rollup. No new migration: migration
035 already carried all six `total_rent/sale_p25/p50/p75_czk`
columns.

- **Orchestrator** in `api/building_runs.py`: `confirm_units`
  flips the row to `estimating` and hands off to
  `_run_building_estimations`, which fans out one rent + one sale
  `estimation_runs` child per confirmed unit, each linked back via
  `building_run_id` + `building_unit_id`. It is a fan-out +
  synchronous watcher, **not** a new LLM loop — each child runs
  through the existing `create_estimation_run` plumbing
  (`background_tasks=None`), so when the loop returns every child
  is terminal and the rollup is exact. Runs as a BackgroundTask
  from the endpoint (handler returns the `estimating` row; the
  detail page polls); runs inline when called without
  `background_tasks` (tests).
  - **Reuse of the apartment estimator skill.** Rent children run
    in **agent mode** under
    `app_settings.building_default_estimator_skill` (seeded by
    migration 036 to `rental_estimator_v1`, operator-tunable via
    `/settings`), so a unit inside a building is estimated exactly
    like a standalone apartment and any skill improvement rolls in
    for free. Children pass `category_main='byt'`,
    `category_type='pronajem'`/`'prodej'`, plus `area_m2` +
    `disposition` from the confirmed unit and `lat`/`lng` from the
    parent parse. **Sale children run in deterministic mode** until
    a sale-specific skill ships; the orchestrator already reads an
    optional `building_sale_estimator_skill` setting (absent today
    → deterministic), so wiring a sale skill later needs no code or
    migration change.
- **Rollup**: `_finalise_building` runs once every child is
  terminal and `_rollup_totals` sums the **successful** children
  into `total_rent/sale_p25/p50/p75_czk`. P50 is a straight sum;
  P25 / P75 sum the per-unit IQR endpoints. A percentile with no
  contributing unit stays NULL rather than reading as a misleading
  zero. The building lands `success` if any child succeeded, else
  `failed`. `sweep_stuck_buildings` now also recovers an orphaned
  `estimating` row (server restart mid-fan-out) to `failed`.
- **Frontend**: `/building/:id` grows a "Building totals" section
  (rent + sale `RangeStrip`s) and a per-unit card list — each unit
  shows its rent + sale estimate strip (reusing `RangeStrip`, the
  same strip `/estimation/:id` uses) and a "View estimate →" link
  to the child estimation. The read-only proposal table stays as
  the pre-fan-out / no-children fallback.
- **Out of scope (carried forward)**: sale-side estimator skill
  (sale children stay deterministic until `sale_estimator_v1`
  ships); the Excel-style business case overlay (B3).

### Phase B3: Business case tab

- Storage: `building_runs.business_case` JSONB (column exists from
  B0). Holds assumptions + floor schedule + unit-schedule overrides
  + computed outputs. JSONB grain because the spreadsheet is
  non-tabular and operator-tunable.
- Math engine: `api/business_case.py` — pure-Python port of the
  `model_Kralupska.xlsx` formulas (~30 lines of Excel logic).
  Stdlib only. Inputs from the column above + the unit list +
  the latest rollup totals; outputs EBIT / EBT / MOIC / IRR /
  yield-on-cost + the per-row breakdowns.
- API: `PUT /buildings/{id}/business_case` (idempotent save +
  recompute); the GET returns the persisted state.
- Frontend: an Excel-like grid as a new tab on the building page.
  Option A: hand-rolled `<table>` + per-cell `<input>`, save-on-blur
  to the PUT. Option B: an off-the-shelf grid (Handsontable
  Community / `react-spreadsheet`) — needs operator approval for the
  new dep. Default recommendation is A on the strength of "no new
  deps without justification"; revisit if the hand-rolled grid
  proves too rigid.

