> Track file — part of [ROADMAP.md](../ROADMAP.md). After shipping, edit only this file + its index row.

## Next

### Real-time program Wave C: the always-on hot lane (greenlit 2026-07-02)

Design: `docs/design/realtime-scrapers.md`. One small always-on worker (second Railway
service, SAME Docker image) replaces cron ticks with continuous loops for the
latency-critical path: newest-first delta probes per portal (2-5 min; live-verified
capability per portal, sreality via count-delta probes pending a BFF sort spike) →
continuous detail drain → per-listing images-first processing (download + INLINE pHash +
warm-CLIP tag; a listing is not published/notified before its first image is stored —
operator decision) → dedup dirty enqueue → matcher wake. Prerequisite: a DB-backed shared
politeness ledger (RateLimiter is per-process today; two runtimes must share one budget
per portal). GH Actions keeps the free heavy lanes (full reconcile walks, backfills, batch
scans, monitors).

**Done (2026-07-04, PRs #694–#697):**
- **W1 (#694)** — per-source drain-disable knob (`realtime_drain_disabled_sources`): freeze a
  portal's queue during a proxy outage instead of burning it to `given_up`.
- **W2 (#695)** — bounded live forensics on the `--free` dedup lanes (`--compare-budget` dirty
  40 / candidates 100 / full 300), closing the steady-state `auto_visual=0` gap so different-photo
  cross-portal apartments auto-merge on the scheduled run (verified: `auto_visual=29` on the first
  post-merge candidate run). **DECISION (operator 2026-07-04): NO batch warmer** — dedup cost is
  pay-at-decision-time (pHash → CLIP cosine → bounded live forensics), the floor-plan gate on its
  OWN budget; `dedup_batches.yml` is dispatch-only. (Supersedes the old "batch-warmer revival".)
- **W3 (#696, migration 270)** — sreality count-probe lane (`pagination.total` deltas → opt-in
  targeted `index_walk`; sreality's v1 API is sort-blind so it can't do a newest-first probe).
- **W4** — idnes/bazos street-coverage measurement: the RÚIAN resolver lever is **exhausted** (coord
  precision ceiling — idnes coords >15 m from address points, bazos coords are shared page-link
  pins); the 61%/51% eligibility is not resolver-fixable without better coord extraction.
- **W5a (#697)** — delisting rails: sreality `INDEX_MIN_COMPLETENESS` 1.0→0.995 + a 3h
  `min_unseen_hours`; the 6 h portals 24h→12h.
- **Geo real-time + scan hygiene (#710, #713, #715)** — stored `listings.geo_cell_key`
  (migration 276); the real-time dirty drain enqueues street-OR-geo and runs a geo sub-pass
  over the claimed cells; the scheduled geo backstop advances a `lane='geo'` cursor in
  `dedup_scan_state` (whole-market cycles instead of head-restarts, on the existing
  wall-clock budget); photo-less eligible properties route to the dirty lane via an
  imageless EVALUATION sweep in the */5 maintenance pass (the engine still decides +
  stamps them — an evaluation trigger, not a publish-timeout).
- **Dedup qualification hygiene (#761, 2026-07-11)** — `publication.GEO_FAMILIES` is the single
  Python source of the geo category list (MatchProfile derives `geo_blocked`; migration
  276's SQL twin is test-pinned); dead `requires_development_guard` flag deleted; the
  dirty-drain claim scoper embeds `_ELIGIBILITY` (parity-tested, no more hand-inlined
  predicate); cross-pass visibility — street-eligible geo-family rows now participate in
  the geo pass (a street-eligible dům can meet its street-less cell peers, ~841 active
  listings were mutually invisible), with both-street-eligible PAIRS skipped so the
  street pass keeps sole ownership.

**Still open:** the Wave C worker prerequisites (create the Railway service, flip
`shared_rate_limiter` on, images-first inline pHash + warm-CLIP + matcher wake); **W5b** (SLO-scaled
health + push alerting + silent-green closures — deferred, see `realtime-scrapers.md` § Sequencing);
the sreality BFF sort HAR spike.

### Phase B1: Building decomposition — URL ingest + unit extractor + confirmation UI (active)

Second slice of the building-paste flow. Builds on B0's persistence:
operator pastes a `dum` (house) or `komercni` URL → backend parses
via the existing dispatcher → an LLM-vision skill reads the
description + floor plans + photos → proposes a unit list → the UI
renders an editable confirmation step → operator confirms. End of B1
the building is in `status='awaiting_input'` until the operator
submits, then advances to `estimating`. B2 picks up the per-unit
estimation fan-out from there.

Full description (including the apartment-skill-reuse note on the
B2 orchestrator step) under "Building decomposition track" below.

Headline scope:
- Migration 036: `building_unit_extractions` cache table
  + `'extract_building_units'` value on `llm_calls.called_for`
  + four `app_settings` rows for the new skill / prompt / model
  (`llm_building_extractor_system_prompt`, `llm_building_extractor_model`,
  `llm_building_extractor_max_images`, `building_default_estimator_skill`).
- New toolkit function `toolkit.building_extraction.extract_building_units`
  — write-allowed exception per toolkit rule #5; same cache pattern as
  `summarize_listing` (keyed on `(sreality_id, snapshot_id)`).
- New skill `building_unit_extractor_v1` (vision extractor, not an
  estimator) — on-disk `skills/building_unit_extractor_v1/SKILL.md`
  + migration seed `INSERT`. Allowed tools: `extract_building_units`
  + `record_building_units` terminator. Distinct from the apartment
  estimator skill — its job is structural extraction only.
- `POST /buildings/from_url` replaces B0's minimal `POST /buildings`
  as the operator-facing entry. Rejects `category_main='byt'` (those
  go through `/estimations`).
- `POST /buildings/{id}/confirm_units` accepts the operator-edited
  unit list, validates, writes to `units`, advances status to
  `estimating`. Idempotency via 409 on non-`awaiting_input` rows.
- Frontend: new `kind` toggle on `NewEstimationModal` ("apartment" /
  "building"), `BuildingUnitEditor` component for the review step,
  new `/building/:id` page (initially read-only — full rollup view
  ships with B2).

### Phase 7 slice 2: Async + full toolkit + UI mode toggle
Builds on slice 1.
- Async execution: real `status='pending'/'running'` lifecycle with
  a background worker and a polling endpoint. Removes the
  synchronous HTTP wall-clock cap.
- Expose the rest of the toolkit (`cluster_comparables`, the two
  velocity tools, the visual layer) by adding skills that whitelist
  them.
- Frontend `/estimate` gets a mode toggle (`deterministic` /
  `agent`), a provider picker (anthropic / gemini), and a skill
  picker.
- Third provider (OpenAI or Vertex AI service-account auth).
- Per-skill A/B comparison view on `/estimations`.

### Phase 7d: Agent code execution (deferred)

Let the agent build and run small ad-hoc Python when the fixed
toolkit can't express a needed calculation (e.g. a one-off
distribution fit, a custom aggregate over the comparables already
in hand, a sensitivity check the existing tools don't cover).
Scoped now, implemented later — sequenced after the manual
rental estimates work (Phase U-ME) so the simpler, contained
schema feature lands first.

Operator-confirmed approach:
- Self-hosted sandboxed subprocess on the Railway container.
  No third-party sandbox (rules out e2b), no provider-hosted
  code-exec beta (rules out Anthropic's `code_execution_20250522`
  and Gemini's native `code_execution`). Cross-provider neutral
  per CLAUDE.md.
- Sandbox primitives still to design when the phase starts:
  `subprocess.run` with `preexec_fn` setting `rlimit_as` /
  `rlimit_cpu`, env scrub, no network egress, per-call tmpdir,
  wall-clock timeout. Or `RestrictedPython` for a pure-Python
  whitelist. Decision is part of the phase, not this stub.
- New `agent_code_executions` audit table keyed on
  `estimation_runs.id` so every code block, its stdout, stderr,
  duration, and result is auditable alongside the existing
  trace.
- Wired as a new `computation_v1` skill rather than a flag on
  `rental_estimator_v1` — keeps the safety boundary explicit
  and avoids broadening the existing skill's allowed_tools by
  a category-change rather than a per-tool addition.
- Trace integration: each execution emits a `step.kind =
  'code_execution'` entry alongside today's `tool_call`,
  `computation`, and `reasoning` kinds. `TRACE_SCHEMA_VERSION`
  in `api/estimation_runs.py` bumps when this lands.
- Open questions deferred to the phase: pre-populated namespace
  shape (pandas-ready vs pure dicts), whether the agent can
  reference earlier tool outputs by name, soft-cost cap per run.

### Phase QUAL: Qualitative city data + population overlay (in progress)

Operator-curated qualitative indexes (employment, safety, services,
amenities, etc. — 33 metrics from `data/obce_v_datech_2025.csv`) for
206 Czech cities, attached to the geo data already on each listing,
plus an authoritative population column sourced from ČSÚ. Both
surfaces feed the Browse filters and the U2.7 notification
subscriptions so an alert can fire when "listing in a city with
employment-index > 5 and population > 20 000" or on a compound
proximity rule ("within 5 km of a city with safety-index > 6,
services-index > X and population > 20 000") — combinable with the
standard listing facets (floor area, disposition, price, price per
m², etc.). Browse map also renders matching cities as a separate pin
overlay that can be heatmap-color-coded by any chosen index.

**What's shipped** (this commit):

- **Schema** (migrations 078 + 079): `curated_cities`,
  `city_index_revisions`, `city_index_values`, `city_index_definitions`,
  `city_population`, plus the three `*_public` views and the
  `listings_with_city_quality(p_index_rules, p_pop_min, p_pop_max,
  p_proximity)` RPC. Anon SELECT on the views and EXECUTE on the
  RPC; SECURITY INVOKER throughout.
- **Backend**: three new filter defs in `toolkit/filter_registry.py`
  (`city_index_rules`, `min/max_city_population`,
  `near_city_proximity`), gated to BROWSE + WATCHDOG agendas only so
  the estimation agent / comparables tool stay unaware.
  `toolkit/comparables._shared_filter_where` and
  `api/notifications._build_match_clauses` both render the new
  clauses by delegating to the shared `_city_quality_clauses` helper
  — Browse and Watchdog stay in lockstep.
- **Browse data path**: `frontend/src/lib/queries.ts` resolves the
  city-quality sreality_id allowlist via the new RPC and AND's it
  alongside the existing tag prefilter — same composition pattern as
  `listings_with_tags`. Map / Table / Cards all honour the new
  predicate without touching the existing PostgREST fast path.
- **Filter UI**: new "City quality" `<ControlGroup>` in
  `Filters.tsx` with the `CityIndexRulesPicker` custom widget
  (dropdown grouped by category × threshold input, repeatable) plus
  range inputs for min / max city population.
- **Map overlay**: `ListingMap.tsx` renders the curated city set as
  a separate GeoJSON layer above the listing dots, with bottom-left
  controls for "Show cities" toggle + "Color by:" dropdown + gradient
  legend. Heatmap paint expression `red(0)→amber(5)→green(10)` matches
  the data's 0–10 index range. Click → popup with city name, kraj,
  population, and every index value (highlighted index pinned to the
  top). **Cities draw as their real municipality boundary polygons**
  (migration 139's `curated_city_polygons_public` — RÚIAN obec geometry
  simplified to GeoJSON, anon-read), not fixed-radius circles: a
  translucent same-tone fill + a thicker conditional-coloured border,
  and **the selected index figure is labelled at each shape's
  centroid** (`city-fill` / `city-outline` / `city-label` layers). A
  city with no boundary falls back to a radius circle.
- **Values fetch un-truncated**: `fetchCityIndexValues` now pages
  through `city_index_values_public` in 1,000-row chunks. PostgREST
  hard-caps responses at the project's `db-max-rows` (1,000), which the
  old `.range(0, 49999)` could not lift — so only the first ~32 cities'
  values reached the browser and every other city (Dobříš included)
  showed em-dashes / a grey, value-less shape. Mirrors the same fix
  `fetchRentMapChoropleth` already used.
- **Obec re-link correction** (migration 140): the polygons exposed 6
  curated cities that migration 081's name-walk had linked to the WRONG
  obec — a larger, differently-named neighbour (Šlapanice→Brno,
  Odry→Ostrava, Hranice/Jeseník→Olomouc, Chrudim→České Lhotice,
  Mělník→Úžice), which drew a giant blob AND mis-scoped their
  `ST_Covers` city-quality filter. Re-linked by exact obec name match
  (tie-broken by nearest centroid); pure spatial containment was unsafe
  because several Mapy.cz centroids land just outside the town in a tiny
  neighbour. 202 correct links + the 20 cities with no same-name obec
  are untouched.
- **Tooling**: `scripts/seed_curated_cities.py` reads the operator
  CSV, geocodes each (Město, Kraj) pair via Mapy.cz, writes to the
  DB. Per-city radius is derived from the Mapy.cz bbox (clamped
  2–25 km). Operator triggers via
  `.github/workflows/seed_curated_cities.yml` (Mapy.cz + Supabase
  secrets, geocode cache committed back to the branch for offline
  reruns). Seed is idempotent: curated_cities upsert by `(name,
  kraj_name)`, definitions upsert by `index_name`, each run appends
  a new `city_index_revisions` row.

**Next-commit follow-ups also shipped**:

- **Watchdog editor surfaces the city-quality section.** The picker
  + 3 numeric fields now render in `WatchdogEdit.tsx` via the same
  custom-widget wire-up Browse uses. `WatchdogFilterSpec` gained
  the four matching fields (`city_index_rules`,
  `min_city_population`, `max_city_population`,
  `near_city_proximity`) and `DEFAULT_WATCHDOG_FILTER_SPEC` gets
  the matching nulls. Picker wire shape unified on snake_case
  `{index_name, op, value}` so the same operator output flows
  unchanged to the Browse RPC, Watchdog matcher, and the new Stats
  RPC.
- **Stats tab honours city-quality filters.** Migration 080 extends
  `browse_stats` to 44 params with the same four city-quality
  inputs the listings RPC accepts. Same EXISTS / NOT EXISTS
  semantics. Aliased the outer SELECT (`from listings_public l`)
  to avoid the bare-`lat`/`lng` ambiguity inside the EXISTS, which
  silently turned the geographic filter into a no-op on the first
  draft. `fetchBrowseStats` now passes the four params; Stats and
  Map/Table can never disagree on a city-quality cohort again.
- **Population fetcher**: `scripts/fetch_population_wikidata.py`
  queries Wikidata's public SPARQL endpoint for every Czech
  municipality's `population (P1082) @ point in time (P585)`,
  matches by `(name, kraj)` against the curated list, and writes
  `data/csu_population_2024.csv` — the file the seed script
  already loads on present. Wired through
  `.github/workflows/refresh_population.yml` for operator-triggered
  refresh; no DB access required (the workflow just regenerates
  the committed CSV).
- **Population source switched to the official ČSÚ DataStat file.**
  The Wikidata fetcher is now a fallback; the preferred source is the
  official export "Počet obyvatel v obcích k 1. 1." (download from
  https://data.csu.gov.cz/datastat/data/VYBER/OBY02AT02). The operator
  commits the downloaded JSON-stat file to `data/csu_population.json`;
  `scripts/csu_population.py` parses it (takes the latest year, derives
  each municipality's kraj from the JSON-stat `child` map, drops the
  kraj-level aggregates) and `scripts/seed_curated_cities.py` matches
  municipalities to curated cities by `(name, kraj)`
  (diacritics-insensitive) and upserts `city_population`. The seed
  prefers the JSON and falls back to the legacy CSV when it's absent.
- **Price-per-m² filter everywhere.** Two new `FilterDef`s in
  `toolkit/filter_registry.py` (`min/max_price_per_m2`,
  `pg_column='price_per_m2'`, all-agenda) make per-m² bounds a
  first-class registry primitive. Toolkit comparables, the Watchdog
  matcher, `EstimationFilters` / `WatchdogFilterSpec` on the TS side,
  Browse URL state, the Filters.tsx Price control, and Stats all
  honour it via one consistent expression
  (`price_czk::numeric / NULLIF(area_m2, 0)`). Migration 083 extends
  `browse_stats` to 46 params so the Stats tab stays aligned with
  Map / Table whenever a per-m² bound is set. The PostgREST direct
  paths get this for free because `listings_public` already exposes
  `price_per_m2` as a computed column.
- **15-minute lightweight delta scrape.** New
  `.github/workflows/scrape_delta.yml` (cron `*/15 * * * *`,
  `--limit 200`, image / condition phases skipped) walks the first
  ~3 index pages of each of the 6 category pairs every 15 minutes so
  a newly-listed sreality property reaches the Watchdog feed within
  minutes instead of within a day. The nightly `scrape.yml` still
  owns `mark_inactive` per architectural rule #3 — the partial walk
  here can never falsely flip a live listing inactive thanks to the
  `--limit`-set guard in `scraper/main.py:main`. Concurrency-group
  drops overlapping runs rather than queueing them.
  _(Superseded by Scraper-track Phase 1.6: this job now does a complete
  walk every tick and runs `mark_inactive` itself.)_
- **Watchdog feed polling decoupled from estimation polling.**
  `frontend/src/pages/Watchdog.tsx` switches from an unconditional
  5-second `refetchInterval` to a two-tier callback: 30 s for the
  dispatches feed (matcher ticks every 5 min, so 30 s gives plenty
  of resolution at 1/6 the request volume), bumped to 5 s only when
  any visible row carries a non-terminal estimation status. Drops
  back the moment estimation completes.

**Still next** (separate slice):

- **`/cities` admin page**: in-app uploader for next year's CSV
  (preview + confirm two-step). Today's flow goes through the
  GitHub Action.
- **Per-city `default_radius_m` rework.** The current value comes
  from each city's Mapy.cz bbox half-diagonal (clamped 2–25 km).
  This works for small towns but is too tight for major cities —
  e.g. Brno comes out at 2 km, well short of Brno's actual
  built-up footprint, so a Brno-rule + Brno listing pair can miss
  unless the listing sits within 2 km of the centroid. The right
  fix is a population-weighted radius (`r ≈ k·sqrt(pop / density)`,
  clamped 2–25 km) once `city_population` is seeded by the
  Wikidata fetcher above. Manual overrides via direct UPDATE on
  `curated_cities.default_radius_m` work today.
- **Prague gap.** The operator's source CSV omits Prague
  (instead carries Prague-Východ / Prague-Západ as suburban
  okres entries). City-quality filters therefore don't activate
  for any Prague-bbox listing today. Adding a manual Prague row
  to `curated_cities` (centroid 14.43,50.07 / radius 18 km) and
  seeding its 33 index values from a separate source is the
  cleanest fix; deferred until the operator decides whether they
  want Prague-as-one or Prague-broken-into-districts.
- **Operator decision still open**: whether to expose the `op`
  operator (currently locked to `>=` in the picker) or keep it
  simple.

Headline scope:
- Migration: `cities(city_id, name, csu_code, geom geography(point,
  4326), centroid_admin_polygon geography(multipolygon, 4326) NULL,
  ...)` — canonical reference table. `city_id` resolved via the Czech
  Statistical Office (ČSÚ) municipality code so successive uploads
  align cleanly.
- Migration: `city_indexes(city_id, source_revision, uploaded_at,
  uploaded_by, raw_row jsonb)` + `city_index_values(city_id,
  source_revision, index_name, value numeric)` — long-form so a new
  index column on next upload doesn't need a schema migration. Append
  -only via `source_revision`; the latest revision is the default
  query target, prior revisions stay auditable.
- Migration: `city_population(city_id, as_of_year, population,
  source)` — one row per (city, year) so historical analysis stays
  possible without breaking the latest-wins norm elsewhere.
- Migration: extend `listings` with `nearest_city_id` (FK to
  `cities`, nullable, backfilled from `geom`) so a per-listing filter
  on city quality avoids a per-query spatial join. Trigger updates it
  on insert / coordinate change.
- Population source: pick one canonical feed during scope review
  (ČSÚ open data, Wikidata SPARQL, or the OSM `admin_centre` tag) so
  numbers don't drift between surfaces.
- Spreadsheet ingest: `POST /admin/cities/indexes/upload`
  (bearer-gated) accepts the operator's CSV / XLSX, validates the
  column set, resolves city rows by ČSÚ code (with a name-fallback
  preview for unmatched rows), writes a fresh `source_revision`,
  returns row-level errors. FastAPI parses — never the browser. Same
  upload-then-confirm pattern as building-unit extraction (Phase B1).
- Browse filters: extend `Filters.tsx` with a "City quality" section
  that enumerates available index names from the latest
  `source_revision` so the UI updates automatically when an upload
  adds an index. A new compound proximity filter ("within X km of a
  city matching Y") is the headline new primitive — backed by a
  single PostGIS query (`ST_DWithin` against the matching cities'
  geoms) rather than UI-side iteration. Reuses the existing
  `_shared_filter_where` helper so the matcher and Browse can never
  disagree on what a filter means.
- Notification integration: the saved-filter spec on
  `notification_subscriptions` (Phase U2.7) extends to accept the new
  city-quality and proximity predicates. The dispatch matcher reuses
  the same SQL builder — one shared definition of "matches."
- Operator surface: `/cities` admin page lists the registered
  cities, current population, and latest index values for sanity-
  checking the most recent upload.

**Open questions (operator to decide before implementation starts)**

- **Canonical city identity.** Match cities by ČSÚ municipality code
  (`obec_kod`), Wikidata Q-id, or our own slug? ČSÚ recommended —
  stable Czech-statistics identifier, joins cleanly to most public
  datasets.
- **Geo definition of "in city X".** Centroid + radius (sloppy at
  city edges, cheap), nearest-city assignment (cheap, defensible),
  or polygon containment using ČSÚ admin polygons (correct, adds a
  one-off shapefile import). Nearest-city via `nearest_city_id`
  recommended as the default; polygon containment can be added later
  without invalidating data.
- **Index schema shape.** Long-form `(city_id, index_name, value)`
  (flexible — recommended) vs fixed columns (cleaner SQL, every new
  index needs a migration). Long-form lets the Browse UI enumerate
  indexes from data rather than schema, matching the
  `app_settings`-style discipline.
- **Population cadence.** Bulk-load once from a static dataset
  (cheaper, drifts) or refresh annually via the same upload endpoint
  (matches the index-upload workflow, no scheduled worker)? Annual-
  upload recommended.
- **Filter UI complexity ceiling.** "Within X km of a city with
  markers A>n, B>m and population>k" is a nested predicate. Cap the
  UI grammar at max-depth-1 nested rules (one outer city criterion +
  one optional proximity criterion); deeper compound rules go via a
  free-form JSON expression on power-user subscriptions only.
- **Snapshot vs live for index values.** If a new `source_revision`
  arrives between a notification being saved and its first dispatch,
  does the dispatch use the spec's revision-as-of-saved or the
  current one? Current-revision recommended (matches the
  latest-wins norm); the alternative is heavier and rarely useful.

**Out of scope for this phase**

- Per-user index overrides (every operator sees the same index
  values; single-operator identity model still applies).
- Automated scraping of index data — input is a hand-curated
  spreadsheet, not an automated feed.
- LLM- or ML-derived quality scores. The indexes are operator-
  supplied facts; any reasoning on top happens at the agent layer
  per toolkit rule #1.
- City-quality features beyond Browse filter / notification matching
  (e.g. ranking the agent's comparables by city quality, surfacing a
  quality badge on Listing Detail, applying city-quality predicates
  to estimation cohorts) — natural follow-ups, not gated by this
  phase. Phase QUAL deliberately does **not** touch the estimation
  agent, the building decomposition flow, or any other surface; its
  scope is the Browse filter primitives and the U2.7 notification /
  watchdog spec.

