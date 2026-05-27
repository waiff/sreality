# CLAUDE.md

Standing context for any future Claude Code session that touches this repo.
Read this before changing anything.

## What this project is

A daily scraper for Czech real estate listings from sreality.cz. Each
nightly run walks all six meaningful category pairs in sequence —
apartments, houses, and commercial properties, each in both rental
and sale variants (defined as `CATEGORIES` in `scraper/main.py`). The
output is a Postgres database (Supabase, Frankfurt region, PostGIS
enabled) with full listing history. Downstream surfaces over the
same data: an analytical toolkit + FastAPI service (Railway) and a
read-only browser UI (also Railway, separate service). Still out of
scope until explicitly opened: ClickUp integration, MCP wrapping the
toolkit, per-user identity.

**Data source (sreality v1 API).** In 2026 sreality rebuilt their site
on Next.js and removed the old `/api/cs/v2/estates` API the scraper was
born on. The scraper now reads the public JSON v1 API:
`GET /api/v1/estates/search` (filters `category_main_cb` /
`category_type_cb` / `locality_country_id=112`, **offset/limit** paging,
`pagination.total` for completeness) for the index, and
`GET /api/v1/estates/{id}` for detail (a `{categoryMainCb, locality,
params{…}, images, price…}` object; `params` holds the typed
attributes). No cookies needed. The deep-pagination cap still applies
(HTTP 422 past the window), so large categories are walked per-district
(`SPLIT_THRESHOLD` / `DISTRICT_IDS`). `parser.parse_listing` maps that
object to the same row contract; `scraper/hashing.py` strips the new
volatile fields (`params.stats` view counter, `note`/`rus`/`rusReply`).

## Territories

The repo is split into two top-level territories with deliberately
different rules. Identify which one a task belongs to before you start.

**Backend territory** (`scraper/`, `toolkit/`, `api/`, `migrations/`,
`tests/`, `.github/workflows/`):
- Python 3.12, stdlib-first, `psycopg` direct to Postgres.
- Service-role database access. Reads and writes anything.
- Runs in GitHub Actions (scraper) or Railway (FastAPI).
- All rules below apply: append-only migrations, snapshot-on-change,
  no deletes, no `supabase-py`, etc.

**Frontend territory** (`frontend/`):
- Browser code. Vite + React 18 + TypeScript + Tailwind v4 SPA, served
  by Caddy from a two-stage Docker build (see `frontend/Dockerfile`).
  Deployed to Railway as a separate service alongside the API.
- The U1a deliverable is a four-page database browser: **Browse**
  (filters → Map / Table / Stats), **Listing Detail** (with snapshot
  timeline strip — the product's signature visual element), **Region**
  (district or radius aggregates), **Health** (operator dashboard).
  The U2 estimation flow extends the same SPA: **Estimate**
  (`/estimate`, two-step form: paste URL → review specs → submit
  goes through the FastAPI service), **Estimations** (`/estimations`,
  list of past runs filterable by source/status), **Estimation
  Detail** (`/estimation/:id`, rent range + warnings + input recap
  + trace timeline + comparables + re-run button). The Timeline
  component dispatches on `step.kind` so it renders today's
  deterministic 4-step traces and the future U4 agent's longer
  traces without rework.
  Future U3 / U4 work extends this UI; do not fork into a
  separate frontend tree.
- Connects with the **publishable (`anon`) key only**. Never embed the
  service-role key, the `SUPABASE_DB_URL`, or any other secret in
  browser-shipped code.
- Reads exclusively from the `*_public` views — migration 008
  (`listings_public`, `listing_snapshots_public`,
  `listing_freshness_checks_public`, `listing_fetch_failures_public`)
  and migration 015 (`images_public`) — and from the page-specific
  RPCs in migrations 011 / 012 / 013 / 014 (`browse_stats`,
  `region_stats`, `region_active_by_day`, `health_summary`).
  All RPCs are `SECURITY INVOKER` and rely on anon's existing SELECT
  grant on the public views — they don't escalate. New public-data
  RPCs follow the same pattern; new private RPCs go through the
  FastAPI service.
- **No write path from the browser.** Any UI action that needs a
  write goes through the bearer-token-gated FastAPI service, not
  direct Postgres. The toolkit's two write-allowed exceptions
  (`verify_listing_freshness`, `find_anchor_amenities`) are reachable
  only via the API.
- **`Mapy.cz`-powered location search.** The Region page calls
  `GET /maps/suggest` and `POST /maps/resolve` on the FastAPI
  service for autocomplete + admin-unit resolution. The
  `MAPY_CZ_API_KEY` is server-side only — never inlined into the
  browser bundle. When the API returns 503 (key unset), the search
  box renders a graceful fallback hint and auto-opens the Advanced
  disclosure with the legacy district / radius pickers.
- Frontend conventions live in `frontend/README.md`. Design tokens are
  in `frontend/src/styles/globals.css` under a single `@theme` block;
  **never tweak these tokens without operator approval** — they
  encode the agreed visual direction (civic-archive feel,
  oxidised-copper accent, borders-only depth, tabular numerals,
  Czech locale formatting). Add new tokens only at the bottom of the
  file with a clear domain-name.
- Backend rules below (psycopg, no `supabase-py`, stdlib-first, etc.)
  do not apply inside `frontend/`.

**Chrome-extension territory** (`chrome-extension/`):
- Manifest v3 browser extension that mounts a yield-scenario panel
  on `sreality.cz/detail/*` pages. Two-entry Vite build
  (`content.js` + `background.js`) plus a copied-over `manifest.json`
  and `icon-128.png`; output lands in `chrome-extension/dist/`.
- **Vanilla TypeScript only — no React, no Tailwind.** The panel
  lives inside a closed shadow root with its own scoped CSS in
  `src/styles.css?inline`. Palette mirrors the SPA's civic-archive
  tokens by hand-coded values (no `@theme` import). Keep the bundle
  small.
- Every network call goes through the background service worker via
  `chrome.runtime.sendMessage` so `host_permissions` covers the API
  origin and the fetch isn't subject to sreality.cz's CORS posture.
  The content script never calls `fetch` directly.
- Build-time secrets: `VITE_API_BASE_URL` + `VITE_API_TOKEN` are
  inlined into `dist/` — same Path 1 security posture as the SPA.
  Ship `dist/` only to trusted operators; never upload to the public
  Chrome Web Store. `chrome-extension/README.md` documents Path 3
  (no embedded token, writes bounced through the SPA) for when a
  publicly-shareable build is needed.
- The extension's origin (`chrome-extension://<id>`) must be added
  to the FastAPI service's `CORS_ALLOW_ORIGINS` env var. Install
  unpacked first, copy the assigned ID from `chrome://extensions`,
  then update the Railway env var.
- Backend rules (psycopg, stdlib-first, etc.) and SPA conventions
  (React, Tailwind, design tokens) do NOT apply inside
  `chrome-extension/`.

When in doubt about which territory a task belongs to, ask the
operator. Don't import frontend deps into the Python tree or vice
versa.

## Operator profile

The owner of this repo is non-technical and works **only** through Claude Code
on the web (claude.ai/code) connected to GitHub. They have no terminal, no
local Python, no local Git.

- Never ask them to run a shell command on their laptop.
- Use GitHub Actions for any execution. Tests run via `.github/workflows/test.yml`,
  the scraper runs via `.github/workflows/scrape.yml`.
- For tasks that genuinely need a browser (Supabase SQL editor, GitHub Settings
  pages), give them click-by-click instructions: which page, which menu, which
  button.
- Define jargon the first time it appears ("upsert," "JWT," "RLS," etc.).

## Database access and Supabase MCP

Claude Code has direct read/write access to the Supabase project via the MCP
integration. Use it for: inspecting the live schema, running SELECT queries to
verify data state, applying migrations, running backfill UPDATEs, and
confirming changes succeeded.

The `migrations/` folder remains the source of truth for schema. Every schema
change still goes in a new numbered SQL file. MCP is the *execution*
mechanism, not a replacement for tracked migrations. Applying a schema change
without committing the corresponding migration file silently breaks the
codebase — future sessions or fresh rebuilds will be missing the change.

Correct flow for any schema change:

1. Write the new numbered migration file (`00N_*.sql`) in `migrations/`.
2. Show the migration to the operator and get explicit approval before running.
3. Apply via MCP (`apply_migration`), verify with a SELECT.
4. Commit the migration file in the same change.
5. Report what was applied and what was verified.

Never apply a SQL change that doesn't correspond to a committed migration
file.

Never run destructive operations (`DROP TABLE`, `DELETE` without `WHERE`,
`TRUNCATE`, `ALTER COLUMN` that changes type or drops a column) without
explicit operator confirmation in chat. "Yes, apply it" is required.

Read-only inspection (counts, sample rows, schema introspection, verifying
backfills) needs no confirmation — just do it and report findings.

The MCP connection points at the production Supabase project. There is no
separate dev/staging database. Treat every operation accordingly.

## Roadmap maintenance

`ROADMAP.md` is the sequencing source of truth. It has two parts:

1. **Auto-status block** (between `<!-- BEGIN AUTO-STATUS -->` and
   `<!-- END AUTO-STATUS -->`) — regenerated at session start by
   `scripts/regenerate_roadmap_status.py`, wired through
   `.claude/settings.json` as a `SessionStart` hook. Counts, last-scrape
   recency, recent commits, migration tally. Never hand-edit; changes
   will be overwritten next session. If `SUPABASE_DB_URL` is not in env
   the block degrades gracefully to "Database unavailable this
   session" — the hook never blocks startup.
2. **Narrative phase entries** (everything else) — manual. After
   shipping meaningful work (a merged PR that completes a phase
   bullet, a new migration, a new toolkit function, a new UI page),
   update the relevant phase entry in the same commit as the work:
   move bullets from `## Next` to `## Done`, add new "next" items if
   scope changed, update the map / scraper / operator-workflow tracks.
   Don't defer roadmap updates to a follow-up commit.

## Architectural rules (do not violate without asking)

1. **The schema in `migrations/` is append-only.** Never modify an existing
   migration. Schema changes go in a new numbered file (`002_*.sql`,
   `003_*.sql`...) and are applied via the Supabase MCP after operator
   approval. See "Database access and Supabase MCP" for the full flow.
2. **Snapshots on content change only.** Never insert into `listings` without
   computing the content hash and inserting into `listing_snapshots` if it
   differs from the most recent snapshot for that listing.
3. **Never delete listings.** Listings that disappear from sreality get
   `is_active=false`. History is sacred. The `is_active=false` inference
   is only valid after a **complete index walk** — a partial walk
   (`--limit N`, `--detail-only`) cannot determine which listings are
   gone. The scraper enforces this: `mark_inactive` is skipped when
   `--limit` is set, and `--detail-only` never reaches the index phase.
4. **`last_seen_at` is driven by index sightings and successful
   detail fetches; failed fetches never touch it.**
   Every existing listing whose id appears in the run's index gets its
   `last_seen_at` bumped before any detail fetches happen. A successful
   detail fetch (cron or on-demand via `freshness_check`) also bumps
   `last_seen_at` as a side effect of `db.upsert_listing` — that's
   real evidence the listing is alive. A *failed* detail fetch must
   not affect `last_seen_at`, otherwise repeated failures would falsely
   flip a still-live listing to `is_active=false`. The `unchanged`
   path of `freshness_check` deliberately does NOT bump `last_seen_at`
   either — for that case the "I confirmed it" signal lives in
   `listing_freshness_checks.checked_at` instead. See architectural
   rule #9.
5. **Failed detail fetches are tracked, not silently dropped.**
   When a detail fetch (HTTP, parse, or DB write) fails, we record it in
   `listing_fetch_failures(sreality_id, attempts, last_error, given_up)`.
   Next run, listings with an active failure row jump to the front of
   `to_refetch` so the per-run cap can't keep deferring them. After 5
   attempts a row's `given_up` flips to true and it falls out of the
   active retry queue (manual SQL un-flip required to retry). On
   successful fetch the failure row is deleted. Inspect with
   `SELECT * FROM listing_fetch_failures ORDER BY attempts DESC`.
6. **Images are downloaded to Cloudflare R2.** v1 only stored URLs; v1.5
   downloads the bytes to an R2 bucket (S3-compatible) so the data
   survives sreality's CDN expiring listing photos. The `images` table
   tracks per-image download state via `storage_path`,
   `download_attempts`, and `last_download_attempt_at`. Image-download
   is a separate phase after the scrape phase; it's a no-op if R2 env
   vars are missing, so a partial deploy never breaks the scrape.
7. **No new dependencies without justification.** Each entry in
   `pyproject.toml` should have a clear reason. Prefer the stdlib.
8. **Latest-wins data model with snapshot history.** The `listings`
   table always reflects the most recent state. Every meaningful
   change appends a row to `listing_snapshots`. Analytical queries
   default to current state for relevance. Estimates that need
   retrospective auditability record the `snapshot_id` of each
   comparable they used — that resolves to the exact JSON the
   estimate relied on, even if the listing has since been updated or
   marked inactive. Avoid building "as-of" semantics into live
   queries; capture snapshot IDs in the estimate response instead.
9. **`listing_freshness_checks` is append-only and ephemeral.** Rows
   older than 30 days are safe to delete. No automated pruning is
   built; manual SQL when the table gets large. The table records
   every on-demand verification triggered by
   `verify_listing_freshness` — its primary purpose is observability
   and per-listing throttling, not history. The primary history
   table is `listing_snapshots`.
10. **`amenities` + `amenity_fetches` are a local OSM mirror, not a
    history table.** Populated by `find_anchor_amenities` on cache
    miss via Overpass. Cache key is `(category, radius_m, exact
    center, fetched_at within TTL)`. POIs accumulate; no automated
    deletion of POIs that have disappeared from OSM (out of scope).
    Manual SQL pruning when the audit table gets large. Categories
    are determined by the *query* that fetched a POI, not the OSM
    tags themselves — `ON CONFLICT (source, source_id)` overwrites
    on subsequent fetches under different categories. The
    canonical category taxonomy lives in
    `toolkit/amenities.CATEGORY_TAGS`; add new categories there.
11. **`transit_lines` + `transit_line_fetches` are a parallel OSM
    mirror for route geometry (migration 028).** Populated by
    `find_comparables_along_axis` on cache miss via Overpass. One
    row per (relation, member way) pair — `source_id` is
    `"relation/R/way/W"` — so a single relation produces N rows of
    clean polylines and a way shared by two relations occupies two
    rows. Avoids the merge ambiguity that bites when a route has
    branches or loops. Cache key is sha256 of the canonicalised
    `(bbox, transport_types)` pair; bbox values are rounded inside
    `_bbox_around` so identical anchor + radius callers share the
    same cache row. TTL default 30 days, matching the amenity TTL.
    Same accumulate-and-prune discipline as amenities; allowed
    transport types are tram / subway / bus.
12. **`estimation_runs` is the single source of truth for every
    estimation.** Every UI/API/ClickUp/agent invocation lands here.
    Synchronous deterministic mode INSERTs once with a terminal
    `status` (`'success'` or `'failed'`); the schema reserves
    `'pending'`/`'running'` for U4's async agent without forcing
    today's code to write twice. Failed runs still persist a row —
    the row IS the audit trail; the endpoint returns HTTP 200 with
    `status='failed'` and `error_message` set. Re-runs INSERT a new
    row with `parent_run_id` set; the original is immutable. Legal
    `source` values today: `'ui'`, `'api'`, `'clickup'` (CHECK
    constraint, not enum — adding more is a single ALTER).
13. **`building_runs` is the parent grouping for the
    paste-a-building workflow.** One row per pasted house listing
    (typically `category_main='dum'`). Children are normal
    `estimation_runs` rows linked back via `building_run_id` (FK,
    `ON DELETE SET NULL` so child estimations survive parent
    cleanup) + `building_unit_id` (stable string ID matching an
    entry in the parent's `units` JSONB). The unit list lives as
    JSONB on the parent — operator-curated, ~5-10 entries, not an
    analytical object. Status flow: `pending` → `extracting` →
    `awaiting_input` → `estimating` → `success` | `failed`. The
    `awaiting_input` pause is the human-in-the-loop gate where the
    operator confirms / edits the agent's tentative unit
    decomposition before per-unit estimates fan out — the explicit
    departure from today's `estimation_runs` single-shot flow.
    `units_proposal` (agent output, append-only after extraction)
    and `units` (operator-confirmed) are kept separate so the
    extractor's original guess is auditable. The business-case
    overlay (Phase B3) lives in `business_case jsonb` on this
    same row.
14. **Condition scoring is two-axis (building + apartment).**
    `listings.condition` (the raw sreality "Stav objektu" enum, ~11
    Czech text values) stays as the source field — it's what
    `listings_public` exposes today and what the legacy filter binds
    against. The two new derived columns
    `listings.building_condition_level` and
    `apartment_condition_level` (integers 1..5, NULL if not yet
    scored) live alongside it, computed by
    `toolkit.condition_scoring.score_listing_condition`. The score
    cache lives in `listing_condition_scores`, keyed on
    `(sreality_id, snapshot_id)` — same auto-invalidation pattern as
    `listing_summaries` / `listing_marker_extractions`. The scorer
    writes the cache row AND updates the two `listings` columns in
    one transaction with a latest-wins guard so a stale-snapshot
    scorer can't overwrite a fresher score. The coarse
    `condition_assessment` produced by `summarize_listing` is for
    cohort skimming, not authoritative filtering — use the new
    columns for that. The 5-level rubric lives in
    `data/condition_rubric_v1.json` (committed) and is loaded into
    `app_settings.llm_condition_rubric` by
    `scripts/seed_condition_settings.py`; the curated marker
    dictionary follows the same pattern via
    `data/condition_markers_curated.json` →
    `app_settings.llm_condition_marker_dictionary`.

## Toolkit and API rules

These rules govern the analytical toolkit (`toolkit/`) and the FastAPI
service that exposes it (`api/`). They do not apply to the scraper.

1. **Tools return facts, not opinions.** No "recommended price", no "this
   looks like a good deal." Tools return data + provenance. Reasoning
   happens at the agent layer.
2. **Standard envelope on every tool's return value:**
   ```python
   {
     "data": ...,
     "metadata": {
       "tool": "tool_name",
       "filters_used": {...},      # echo of actual params after defaults applied
       "result_count": int,
       "queried_at": iso8601,
       "data_freshness": iso8601,  # max(last_seen_at) of considered listings, or null
     }
   }
   ```
3. **Every tool excludes `given_up = true` listings** from
   `listing_fetch_failures` by default. An `include_unreliable: bool = False`
   parameter overrides.
4. **"Active" filter is `is_active = true AND last_seen_at > now() - interval
   'X days'` (default 7).** Don't trust `is_active` alone — a listing not
   seen for 30 days is functionally inactive.
5. **No writes from the toolkit, with six explicit exceptions.**
   Read-only by default. The exceptions are:
   - `verify_listing_freshness` (and `scraper.freshness.freshness_check`
     that it wraps), which exists so an agent can confirm a comparable
     is still valid before relying on it. Every call logs to
     `listing_freshness_checks` for observability and may also write a
     new `listing_snapshots` row, flip `listings.is_active`, or both.
   - `find_anchor_amenities`, which writes to the OSM-mirror tables
     `amenities` and `amenity_fetches` on a cache miss. POI facts live
     in OpenStreetMap, not our scrape, so we cache them locally to
     keep repeated lookups fast and Overpass-friendly. The cache is a
     pure mirror — no derived analytical state lives in those tables.
   - `find_comparables_along_axis`, which writes to the OSM-mirror
     tables `transit_lines` and `transit_line_fetches` (migration 028)
     on a cache miss. Same rationale as `find_anchor_amenities`:
     transit-route geometry lives in OSM, not our scrape. The cache
     key is `(bbox, transport_types)`, hashed canonically so two
     callers using identical params share the same cache row.
   - `summarize_listing`, which writes a structured Claude summary of
     a listing snapshot to `listing_summaries` (keyed on
     `(sreality_id, snapshot_id)`) on cache miss. Same rationale as
     the OSM mirror: the LLM is the source of truth for the summary;
     we cache locally to keep repeat lookups fast and Anthropic-
     friendly. Auto-invalidates when a new snapshot is recorded.
   - `compare_listing_images`, which writes the structured pairwise
     visual comparison to `listing_image_comparisons` (keyed on the
     canonical-ordered pair) on cache miss. Vision is materially more
     expensive than text, so caching matters more here than anywhere
     else in the toolkit.
   - `read_floor_plan`, which writes a structured Claude-vision
     analysis of one operator-supplied attachment (floor plan, drawing,
     photo) to `building_attachment_analyses` (keyed on
     `(attachment_id, model)`) on cache miss. Same rationale as
     `compare_listing_images`: vision is expensive, the LLM is the
     source of truth, the cache key includes the model so a bump
     invalidates automatically. Only callable inside the building
     flow; the agent handler in `api/agent.py` enforces that the
     `attachment_id` belongs to the run's `building_run_id`.
   - `discover_condition_markers`, which writes a structured list of
     Czech "condition markers" (technical-state phrases like
     "zateplená budova" or "po kompletní rekonstrukci") to
     `listing_marker_extractions` (keyed on `(sreality_id, snapshot_id)`)
     on cache miss. Phase A of the building/apartment condition-
     scoring feature: feeds the one-off marker dictionary that
     `score_listing_condition` reads. Same rationale as
     `summarize_listing` / `compare_listing_images`: vision-augmented
     extraction is expensive, the LLM is the source of truth, the
     cache auto-invalidates when a new snapshot is recorded.
   - `score_listing_condition`, which writes per-listing
     building/apartment condition levels (1..5) + marker_ids matched +
     per-axis confidence to `listing_condition_scores` (keyed on
     `(sreality_id, snapshot_id)`) AND updates the two derived columns
     on `listings` (`building_condition_level`, `apartment_condition_level`)
     in the same transaction, guarded by a latest-wins subquery so a
     stale-snapshot scorer can't overwrite a fresher score. Phase B of
     the condition-scoring feature; reads the rubric + marker
     dictionary from `app_settings.llm_condition_rubric` and
     `app_settings.llm_condition_marker_dictionary` (populated by
     `scripts/seed_condition_settings.py`). Auto-invalidates on new
     snapshot — same pattern as the other vision-augmented analytical
     tools.
   No other toolkit function may write. The API service should still
   connect with a read-only role if Postgres permits; these eight paths
   then need a separately-elevated route. For now we ship with one
   role and discipline.
6. **Spatial queries use `geography(point, 4326)`.** Always
   `ST_DWithin(geom, target_geom, radius_m)`. Never compute distance in
   Python.
7. **psycopg directly, not supabase-py.** Same reasoning as the scraper.
   `prepare_threshold=None` for pgbouncer-mode pooler.
8. **API auth gated by `API_TOKEN`.** When the env var is set, every
   endpoint except `/health` and `/admin/*` requires
   `Authorization: Bearer <token>`. When unset (local development) the
   gate is a no-op. `/health` stays open so Railway healthchecks keep
   working. `/admin/*` (Settings-page surface: skills, `app_settings`,
   agent tool inventory) is also exempt: per Phase 7 slice 1 the
   operator chose to skip per-page auth, so the private Railway URL
   is the security perimeter for that prefix. Every other route still
   requires the token; *no* write path bypasses the FastAPI service.
   The token is shared with every caller; no per-user identity layer.
9. **Trace format on `estimation_runs.trace` is versioned.**
   `TRACE_SCHEMA_VERSION` lives in `api/estimation_runs.py`; every
   row's `trace.version` matches that constant at write time. Shape:
   `{version, summary, steps: [{n, kind, started_at, duration_ms,
   output_summary, ...}]}`. Step `kind` ∈ `'tool_call' | 'computation'
   | 'reasoning'`. The reasoning kind is emitted per LLM turn by the
   Phase 7 agent loop. Steps NEVER store full tool outputs — only
   `output_summary`; the full data lives in dedicated columns
   (`comparables_used` for the cohort, etc.). This caps row size at
   single-digit kilobytes regardless of cohort size. Bumping the
   version is a deliberate change; future readers must handle older
   versions.
   Full per-step tool outputs that the operator may want to drill
   into later live in a separate side-table `estimation_trace_payloads`
   (migration 043, Phase AI slice A), keyed on
   `(estimation_run_id, step_n)`. Populated only for `tool_call`
   steps that the agent loop or `estimate_yield` opts in via
   `StepHandle.set_full_output(...)`. Reachable through
   `GET /estimations/{id}/trace/{n}/payload` for click-to-expand
   drill-down. Same retention discipline as
   `listing_freshness_checks`: rows older than 30 days are safe to
   delete; no automated pruner; manual SQL when the table grows.
   Removing an old payload row strips the drill-down for that step
   but leaves the trace summary intact.
10. **Agent skills live in the `skills` table; the on-disk
    `skills/<name>/SKILL.md` file is the canonical seed.**
    Each skill is a bundle of (system prompt + allowed tool whitelist
    + per-provider preferred model + loop limits). Migration 029's
    seed `INSERT` is the importer of the markdown file's content; at
    runtime the DB row is the source of truth. Operators edit via
    `/settings` (UI) or `PUT /admin/skills/{name}` (API). Every
    update writes a `skills_history` row via trigger — same pattern
    as `app_settings_history` (migration 020). When adding a new
    skill: commit a new `skills/<name>/SKILL.md`, write the
    corresponding seed `INSERT` in a new migration, apply.
11. **LLM provider is pluggable; `llm_calls.provider` records which
    backend served each call.** `api/providers/` defines a
    `CompletionProvider` Protocol with neutral message / tool /
    completion types; today `anthropic` and `gemini` are wired up.
    Adding a third provider is a new file implementing the same
    Protocol, registered in `api/dependencies.py:_build_providers`.
    `LLMClient` is the audit orchestrator — every call writes one
    row to `llm_calls` with provider, model, tokens, USD cost.

## Database access

We connect directly to Supabase Postgres using `psycopg` v3, not the Supabase
REST client. This was a deliberate choice for two reasons:

- PostGIS support: inserting `geography(point, 4326)` is one line of SQL with
  `ST_SetSRID(ST_MakePoint(lon, lat), 4326)`. Doing the equivalent through
  PostgREST requires a stored procedure or fragile GeoJSON casting.
- Atomic transactions: writing `listings`, `listing_snapshots`, and `images`
  for a single listing happens inside one transaction. The REST client cannot
  span tables atomically.

Do not introduce `supabase-py` without an explicit reason and a discussion.

## Auth and secrets

Nine env vars (all GitHub Actions secrets in production):

Database:
- `SUPABASE_URL` - public project URL.
- `SUPABASE_SERVICE_ROLE_KEY` - the new 2025 `sb_secret_...` token.
  **Not** a JWT. The env var name is preserved for forward compatibility;
  the v1 scraper does not actually need it because we connect to Postgres
  directly.
- `SUPABASE_DB_URL` - Postgres connection string from
  Supabase Project Settings -> Database -> Connection string -> Transaction
  pooler (port 6543). Contains the database password embedded in the URL.

Image storage (Cloudflare R2, S3-compatible):
- `R2_ACCOUNT_ID` - 32-char hex from the Cloudflare dashboard.
- `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` - generated when creating
  an R2 API token with Object Read & Write scope on the bucket.
- `R2_BUCKET_NAME` - usually `sreality-images`.

If any R2_* var is missing the image-download phase logs a skip and
exits zero. The scrape still records image URLs in the database;
downloading is decoupled and can be backfilled later.

LLM-backed parsing + agent (FastAPI service only):
- `ANTHROPIC_API_KEY` - Anthropic API key. Required for the URL
  parser, the summarize / vision tools, and the Phase 7 agent when
  it runs under `provider='anthropic'`. Every call is logged to
  `llm_calls` with token counts and USD cost.
- `GEMINI_API_KEY` - Google AI Studio API key
  (https://aistudio.google.com/apikey). Required for the Phase 7
  agent when it runs under `provider='gemini'`. A request that
  selects an unconfigured provider returns a 502 with a clear
  ProviderError message; missing the key at boot is not fatal.
- `MAPY_CZ_API_KEY` - Mapy.cz REST API key. Used to geocode locality
  strings from non-sreality listings, which rarely include coordinates
  on the page.
- `LLM_DAILY_COST_WARN_USD` (optional, default `5.0`) - soft warning
  threshold across ALL providers. When today's `llm_calls.cost_usd`
  sum first crosses this value, the LLMClient logs one WARNING line;
  subsequent calls today do not re-warn. Each provider's own console
  spend cap (Anthropic, Google Cloud billing) is the hard guard; this
  is just an early-warning signal in Railway logs.

All API keys are backend-only. Never `VITE_*` prefix; never expose
to the browser. The `frontend/` build does not see them.

Never write any of these values into a committed file. `.env` is gitignored.
Always reference secrets by env-var name in code.

## LLM-backed parsing

`scraper.source_dispatcher.parse_listing_url` is the single entry
point for any listing URL (sreality or otherwise). It classifies the
URL by domain and routes to either the deterministic sreality flow
(`scraper.url_parser`, unchanged) or an LLM-driven per-source parser
under `scraper/source_parsers/`. Today's allowlist is bezrealitky,
reality.idnes, and remax-czech; everything else falls through to a
best-effort generic parser that always reports
`parse_confidence='best_effort'`.

The LLM path:
1. Cache check against `parsed_url_cache`. Key is sha256 of the
   canonicalised URL (lowercase scheme/host, no query, no trailing
   slash). Hit → return cached spec, no LLM, no cost.
2. Fetch HTML, send to Claude with the system prompt from
   `app_settings.llm_parse_system_prompt` and the per-source user
   prompt from `scraper.source_parsers.<source>`. The model is
   `app_settings.llm_parse_model` (default `claude-sonnet-4-5`).
3. The LLM is required to invoke `record_listing` exactly once with
   every field in a `{value, confidence}` envelope. Any deviation
   raises `ParseError` and surfaces as a 502 from /estimations/preview
   or a `failed` row from POST /estimations.
4. If the page didn't yield lat/lng, geocode the locality string via
   Mapy.cz (`scraper.geocoding`). The geocode confidence rolls into
   `parse_confidence_per_field['lat'/'lng']`.
5. Store the full extraction + spec + warnings in `parsed_url_cache`
   with a 7-day TTL.

Operator-tunable parser behaviour lives in `app_settings`. Editing
the system prompt or model name in that table changes parser
behaviour for the next preview / estimation that hits a non-sreality
URL — no deploy needed. Every prior value is preserved in
`app_settings_history` via the trigger from migration 020.

Cost discipline: every Anthropic call is recorded in `llm_calls`
with token counts (including cache-read / cache-write splits), USD
cost, duration, and the optional `estimation_run_id` of the run that
triggered the call. The `LLMClient` emits a one-time WARNING per day
when `llm_calls.cost_usd` sum first crosses
`LLM_DAILY_COST_WARN_USD` (default $5).

## LLM-backed analysis (visual layer)

Two analytical toolkit functions also reach for Claude (Phase 6,
migration 027):

- `summarize_listing` produces a structured Czech-real-estate Claude
  summary of one listing snapshot. Fields: `headline`, `key_highlights`,
  `concerns`, `condition_assessment`, `target_audience`. Cached in
  `listing_summaries` keyed on `(sreality_id, snapshot_id)`; a new
  snapshot gets a fresh summary on next call. System prompt and model
  ID are operator-tunable via `app_settings.llm_summary_system_prompt`
  and `llm_summary_model`. Calls log to `llm_calls` with
  `called_for='summarize_listing'`.
- `compare_listing_images` scores two listings across six fixed
  tenant-relevant dimensions (`exterior`, `kitchen`, `windows_and_light`,
  `floor_finish`, `lighting`, `styling`) using Claude vision. Image
  bytes are pulled from R2 server-side via boto3 GetObject and base64-
  encoded into the messages payload (more robust than depending on
  bucket public access). Cached in `listing_image_comparisons` keyed
  on the canonical-ordered pair. Operator-tunable settings live in
  `app_settings.llm_image_compare_system_prompt` and
  `llm_image_compare_model`. Calls log to `llm_calls` with
  `called_for='compare_listing_images'`. Vision is materially more
  expensive than text — typical pair runs at ~$0.05 — so the cache
  matters more here than anywhere else in the toolkit.

Both functions are write-allowed exceptions per toolkit rule #5
(see "Architectural rules" above).

## Coding conventions

- Python 3.12. Type hints on every function signature.
- Prefer the stdlib. Reach for a dependency only when stdlib is awkward.
- No comments unless the WHY is non-obvious. Don't narrate WHAT the code does.
- No multi-paragraph docstrings. One-line docstrings are fine for module heads.
- `requests` for HTTP, `psycopg` for DB. Don't add `httpx`, `aiohttp`,
  `sqlalchemy`, or `supabase-py` without a strong reason.
- Keep files small and single-purpose: `sreality_client.py` is HTTP only,
  `parser.py` is JSON-to-row mapping only, `db.py` is database I/O only.

## Adding a new scraper field without breaking existing data

1. Add the column with a new numbered migration (`alter table listings add
   column ...`). Never touch `001_initial.sql`.
2. Update the parser in `scraper/parser.py` to extract the field.
3. Update the upsert in `scraper/db.py` to include the new column.
4. Backfill old rows: either leave them NULL (acceptable if the column is
   nullable) or run a one-off SQL update from the `raw_json` column, which
   already contains the full source record.

## How to test changes

- Push to a branch. `.github/workflows/test.yml` runs pytest on every push.
- For end-to-end testing without polluting the DB: use `--dry-run`
  (logs what would be written, writes nothing).
- For testing a single listing: `--detail-only <sreality_id>`.
- For a small live run: `--limit 10` (caps at 10 listings).

## Refreshing per-source HTML fixtures

The LLM-driven parsers (`scraper/source_parsers/`) are tested against
saved listing HTML in `tests/fixtures/source_html/`. Real listings
get taken down or change layout, so every few months the fixtures
need a refresh. Don't fetch live in tests — that would burn LLM
credit and break offline runs.

Refresh procedure (operator):
1. GitHub repo → **Actions** tab → **Fetch + anonymize source HTML
   fixtures** workflow → **Run workflow**.
2. Pick the branch you want the fixtures on.
3. Optionally edit the URLs (defaults are baked in for the three
   allowlisted sources). Leave a field blank to skip that source.
4. **Run workflow**. It fetches each URL, runs the anonymization in
   `scripts/fetch_and_anonymize_fixtures.py`, and commits the
   resulting `*_sample.html` files back to the same branch.
5. The skipif tests in
   `tests/scraper/test_source_parsers/test_real_fixtures.py` light up
   automatically once the files exist.

Anonymization scope: phones → `+420 XXX XXX XXX`, emails →
`agent@example.cz`, street numbers (`123/45`) → `XXX/YY`. Listing
prices and the surrounding HTML structure are preserved — they're
public data and the parsers need them. Agent names are too varied
to scrub by regex; if a fixture leaks one, hand-edit the file.

## How to manually trigger the scraper

GitHub repo -> **Actions** tab -> **Scraping: Sreality nightly deep run**
workflow (`scrape.yml`) -> **Run workflow** button -> pick branch and optional
flags -> **Run workflow**. (All three scraping workflows are prefixed
`Scraping:` in the Actions list so they group together.)

The scrape runs on a two-tier cadence. **Both tiers now do a complete
index walk** — the split is about how much expensive work each does, not
walk depth:

- **Scraping: Sreality full index walk** (`scrape_delta.yml`, cron
  `0 * * * *`) — the primary scrape. Walks the **entire** index of
  every category pair (no `--limit`), so newly-listed properties surface
  AND delistings flip to `is_active=false`. Because the walk is complete
  it runs `mark_inactive` every run. Detail refetches and image downloads
  are **capped per run** (`--max-detail-refetches 150`,
  `--max-image-downloads 2500`) so it stays bounded; deferred work drains on
  the next run (failure-priority retry + newest-first image ordering). The
  image cap is set above the hourly new-image intake (~2k/h) so the
  newest-first ordering keeps freshly-scraped listings image-complete each
  run; the 2-hourly `images.yml` backfill drains the historical pending set.
  Detail fetches run on a small thread pool paced by a shared rate limiter
  (`--detail-workers` / `--detail-rate`). Records as `run_type='delta'` via
  `--run-type`. Skips condition scoring.
  **Cadence:** the cron is **hourly** (`0 * * * *`), deliberately — each run
  is a complete walk taking 10-16 min, and hourly keeps a steady, polite
  request volume against sreality (a too-aggressive schedule is a plausible
  abuse-flag trigger). GitHub also throttles scheduled workflows (worse
  overnight), so effective cadence can be slightly slower; the Health
  liveness check is tuned to this (warn >90 min, fail >180 min). For tighter,
  near-back-to-back cadence, set the optional `SCRAPE_CHAIN_TOKEN` PAT secret
  (fine-grained: this repo, Actions read+write): the workflow's "Chain next
  run" step then re-dispatches itself on success (GITHUB_TOKEN can't, GitHub
  blocks recursion). The hourly cron remains the safety net that restarts the
  chain. No-op without the PAT.
- **Scraping: Sreality nightly deep run** (`scrape.yml`, cron `0 22 * * *`) —
  the deep nightly. Also a full walk, but its distinct value is the expensive
  backlog work the hourly ticks skip: the condition-scoring phase (LLM
  cost), a deep image-backlog drain (cap 50 000), and a high-cap detail
  catch-up (cap 10 000). Records as `run_type='full'`.

The image backfill (`images.yml`, `--images-only`) is NOT a scrape run and
does **not** write a `scrape_runs` row — only index walks do — so "last
scrape", the liveness check, and reconciliation track real walks.

`mark_inactive` is no longer nightly-only. Two safety rails make the
every-run flip safe (architectural rule #3): (1) each per-category
flip is gated on **walk completeness** — `_walk_complete` compares the
collected count against the API's `result_size` and skips the flip
(logging `INACTIVE skipped`) when the walk looks truncated; (2) a gone
detail fetch (HTTP 404/410 or sreality's "tato stránka neexistuje" body,
surfaced as `ListingGoneError`) flips that single listing immediately and
clears any `listing_fetch_failures` row, instead of accumulating failures.
The `--limit` guard still short-circuits `mark_inactive` for ad-hoc
partial runs.

Concurrency for the 15-min job is `cancel-in-progress: false` — a long
tick is never killed mid-walk; the next cron tick queues behind it
instead of overlapping. Per-category marking commits immediately after
each category's walk, so even a timed-out tick leaves a consistent
partial result. The nightly owns the 22:00 UTC slot.

## Reading the logs

The scraper emits structured progress lines:

- `INDEX offset=N estates=M total=K` per search page (offset/limit paging)
- `INDEX total=N pages=M` once at end of index walk
- `PLAN unchanged=N refetch=M` once after deciding what to fetch
- `PLAN priority_retry=N` once if any listings have prior failure rows
- `PLAN cap=N deferred=M` once if the per-run refetch cap kicks in
- `DETAIL starting refetch=N workers=W` once before the refetch loop
  (detail fetches run on a `W`-thread pool paced by a shared rate limiter;
  DB writes stay serial on the main thread)
- `DETAIL progress=N/M new=... updated=... gone=... errors=...` every 50 refetches
- `RATE penalize status=429|403 url=...` when sreality throttles us and the
  limiter widens its interval (auto-recovers on subsequent healthy fetches)
- `DETAIL id=... new|updated|unchanged` per refetched listing
- `IMAGE id=... inserted=N` per listing with new image rows recorded
- `DETAIL id=... gone (is_active=false)` per listing whose detail fetch
  reported it delisted (404/410 or the not-found body)
- `INACTIVE cm=... ct=... marked=N collected=M result_size=K` per category
  after a completeness-checked mark_inactive
- `INACTIVE skipped cm=... ct=...` per category whose walk looked truncated
  (flip suppressed to avoid false delistings)
- `RUN done pages=... new=... updated=... unchanged=... gone=... errors=...`
- `IMAGES pending=N cap=N workers=N` once before the image-download phase
- `IMAGES progress=N/M ...` every 50 images during the phase
- `IMAGES done downloaded=... errors=... attempted=...` after image phase

A run ending with `errors > 0` is not necessarily a failure (single-listing
fetch errors are tolerated). A run that did not emit a `RUN done` line is
a real failure - check the GitHub Actions log for a stack trace.

## What is explicitly out of scope right now

- Frontend (React, HTML, Lovable, anything user-facing).
- Yield-calculation API.
- ClickUp integration.
- Slack/email notifications.
- Authentication or user management.
- Public read API.

Do not start any of these without explicit user direction in a new session.

## Follow-ups (deferred)

- **Toolkit / API / frontend defaults still target apartment rentals.**
  The scraper was expanded to collect all six category pairs (byt /
  dum / komercni × pronajem / prodej), and migration 022 added the
  ten category-relevant columns the schema was missing
  (`estate_area`, `usable_area`, `garden_area`, `category_sub_cb`,
  `furnished`, `terrace`, `cellar`, `garage`, `parking_lots`,
  `ownership`). Toolkit / API / frontend now accept all of those as
  filters, but the **defaults** still hardcode `category_main="byt"`
  / `category_type="pronajem"`. Specifically:
  `toolkit/comparables.py` (the `category_main` / `category_type`
  defaults on `ComparableFilters`); `api/schemas.py` (the same
  defaults on `FindComparablesIn`, `DescribeNeighborhoodIn`,
  `ComputeMarketVelocityIn`, `CreateEstimationIn`, `EstimateYieldIn`);
  the frontend's "Apartment" labelling in `EstimateForm.tsx` and the
  rental-URL placeholder in `UrlScrapeStep.tsx`. Resolve when a
  downstream surface (UI page, agent flow, ClickUp integration) needs
  to operate over sales / houses / commercial without the caller
  having to override the default each time.

## Schema conventions

- Sreality enum codes that we promote to typed columns are stored as
  Czech text labels without diacritics, mirroring the existing
  treatment of `category_main` / `category_type`. Source maps live
  next to the parser: `parser.CATEGORY_MAIN`, `parser.CATEGORY_TYPE`,
  `parser.FURNISHED`, `parser.OWNERSHIP`. Unknown source codes
  (including sreality's `0` "not specified") return `None`, never
  raise — same forgiving pattern that lets the parser tolerate
  sreality adding a new code (as it did for `category_type_cb=4` /
  `'podil'`).
- `has_balcony` / `has_parking` are LEGACY combined booleans. They
  conflate balcony+terrace+loggia and parking+garage respectively.
  The granular columns added in migration 022 (`terrace`, `garage`,
  `parking_lots`) are the correct fields for new analytical work.
  The legacy columns stay populated for backward compatibility with
  existing queries / RPCs.
