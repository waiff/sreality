# Analytical / agent phases

Core analytical phases — toolkit, freshness, neighborhood, velocity,
spatial context, statistical refinement, visual layer, the reasoning
agent. Each phase builds on the previous; tools within a phase are
independent.

## Done

### Phase 1: Scraper
Daily index + on-demand detail scrape of sreality.cz. Image mirroring to
Cloudflare R2. Failure tracking with give-up threshold. Two-mode GitHub
Actions workflow (conservative cron, opt-in aggressive bootstrap).

### Phase 1.5: Six-category coverage
`CATEGORIES` in `scraper/main.py` walks all six byt / dum / komercni ×
pronajem / prodej pairs in sequence. Per-category refetch cap so a
flooded sale category can't starve the rental walk. `category_type_cb=4`
maps to `'podil'` (fractional ownership). PRs #30, #31. Houses and
commercial listings now accumulate in the database alongside apartments.

### Phase 2: Toolkit foundation
Pure-function analytical tools over the existing schema, exposed as a
FastAPI service deployed to Railway.
- `find_comparables`: parameterised spatial+attribute search.
- `analyze_distribution`: descriptive stats over a cohort.
- `/estimate_yield`: composite endpoint with confidence and warnings.

### Phase 2.5: Freshness layer
Audit trail and on-demand verification.
- `verify_listing_freshness`: throttled re-fetch + snapshot diff.
- `compare_snapshots`: per-listing evolution analysis.
- Snapshot IDs and data-age statistics in the `/estimate_yield` response.

### Phase 3a: Neighborhood, outliers, security
- `describe_neighborhood`: dispositional/price/condition profile with
  trend.
- `find_distribution_outliers`: outlier detection with cross-referenced
  reasons.
- API auth via `API_TOKEN`.

### Phase 3b: Velocity
- `compute_market_velocity`: TOM stats and trend for a filtered cohort,
  with active/delisted/all population control.
- `compute_listing_velocity`: percentile and classification
  (fast/typical/slow/stuck) of a single listing within its peer cohort.
- Shared `_shared_filter_where` helper extracted from `find_comparables`
  so spatial+attribute filter semantics live in one place.

### Phase 4a: Spatial context — anchor amenities
- `find_anchor_amenities`: OSM POI lookup with local cache mirror in
  the `amenities` + `amenity_fetches` tables (cache-key = category +
  radius + center + TTL). Live behind the API; one of the two
  toolkit write-allowed exceptions per CLAUDE.md.

### Phase estimation-4: Generic URL parser
Cross-listed under the UI track for the full detail. Headline:
migration 020, `api/llm_client.py`, `scraper/source_dispatcher.py`,
per-source parsers (`bezrealitky`, `idnes_reality`, `remax`,
best-effort `generic`), 7-day URL cache, daily cost soft-warning.

### Phase estimation-5: URL-parser frontend
`ConfidenceIndicator`, `previewListingUrl`, `useUrlPreview`, listing
block + `force_refresh` + `cost_usd_total` surfacing on `/estimate`.
Commits `e9da41f`, `65b9967`, `d66da7e`.

### Phase 5: Statistical refinement
Two pure-Python analytical toolkit functions, both prerequisites for the
Phase 7 reasoning agent. Stdlib-only (no sklearn/numpy) per CLAUDE.md
"prefer the stdlib" rule.
- `cluster_comparables` (`toolkit/clustering.py`): k-means submarket
  detection over a listings cohort. Stateless — takes the listings
  list returned by `find_comparables` (or any compatible shape).
  Z-score normalises each axis so multi-axis runs aren't dominated by
  absolute scale, runs Lloyd's algorithm with `n_restarts` deterministic
  seeds, picks the lowest-inertia result, de-normalises centroids back
  to original units. Axes: `price_per_m2`, `price_czk`, `area_m2`,
  `distance_m`. Returns clusters sorted by size desc with per-axis
  min/median/mean/max statistics and the list of `sreality_ids` in
  each.
- `find_comparables_relaxed` (`toolkit/comparables.py`): auto-widening
  wrapper around `find_comparables` with full provenance. Runs the
  strict query first; if `result_count < min_results` walks a
  deterministic ladder of relaxations (`radius_x1.5` →
  `area_band_+0.10` → `disposition_loose` → `radius_x2` →
  `area_band_+0.20` → `disposition_any` → `drop_condition` →
  `drop_building_type` → `drop_energy_rating` → `drop_floor_band`)
  until the cohort hits `min_results` or the ladder is exhausted.
  Locality, category, price bounds, and `active_only` are never
  relaxed — they encode user intent. Each intermediate step is
  recorded in `data.relaxation_trace` with the action name, full
  filters snapshot, and resulting count. Caller can override the
  ladder.
- Two new POST endpoints `/tools/cluster_comparables` and
  `/tools/find_comparables_relaxed`, bearer-token-gated. The cluster
  endpoint takes no DB connection (stateless).
- No `estimate_yield` auto-fallback — both tools are standalone, the
  Phase 7 agent opts in. Existing deterministic estimation trace
  remains unchanged.

### Phase 6: Visual layer
Two LLM-backed analytical toolkit functions for the Phase 7 agent:
- `summarize_listing` (`toolkit/summaries.py`): structured Claude
  summary of a single listing snapshot — `headline`,
  `key_highlights`, `concerns`, `condition_assessment`,
  `target_audience`. Cached in `listing_summaries` keyed on
  `(sreality_id, snapshot_id)`; auto-invalidates when content
  changes (new snapshot → new key).
- `compare_listing_images` (`toolkit/image_similarity.py`):
  pairwise visual similarity via Claude vision, scored across six
  fixed tenant-relevant dimensions (`exterior`, `kitchen`,
  `windows_and_light`, `floor_finish`, `lighting`, `styling`) plus
  an `overall_similarity` rollup. Image bytes pulled from R2
  server-side via boto3 GetObject, base64-encoded into the vision
  payload. Cached in `listing_image_comparisons` keyed on the
  canonical-ordered pair.
- Migration 027 adds the two cache tables, extends
  `llm_calls.called_for` with `'compare_listing_images'`, and seeds
  `app_settings` with the operator-tunable system prompts and model
  IDs (`llm_summary_*`, `llm_image_compare_*`).
- New POST endpoints `/tools/summarize_listing` and
  `/tools/compare_listing_images`, bearer-token-gated.
- CLAUDE.md toolkit rule #5 grows from two to four write-allowed
  exceptions (same rationale as `find_anchor_amenities`'s OSM
  mirror: the LLM is the source of truth, we cache locally to keep
  repeat lookups fast and Anthropic-friendly).

### Phase 4b: Spatial context (tenant-perspective overlays)
Two narrow toolkit functions on top of the OSM amenity + transit
caches.
- `compute_walkability` + `compute_amenity_supply`
  (`toolkit/walkability.py`): both project the POI cohort returned
  by `find_anchor_amenities` onto a different signal. Walkability is
  a single 0-100 score driven by weighted nearest-POI distance.
  Supply is the per-category count expressed as a ratio against a
  target count, bucketed `scarce|adequate|abundant`. Two facts, two
  tools, the agent picks. Hermetic tests mock the amenity delegate
  so the math is exercised without an OSM round-trip.
- `find_comparables_along_axis` (`toolkit/transit_axis.py`):
  comparables in a corridor along a tram / subway / bus route. Two-
  stage spatial filter — first find route relations passing within
  `anchor_radius_m` of the target, then return listings within
  `corridor_m` of any of those routes. Reuses the shared comparables
  attribute filters; replaces the anchor-circle ST_DWithin with the
  corridor join. Per-listing output names the nearest line and
  distance to it.
- Migration 028 adds the `transit_lines` + `transit_line_fetches`
  cache tables (one row per relation/way pair, sha256
  bbox+transport_types cache key). The Overpass client gets a
  `fetch_routes` method that parses route relations into clean
  polylines.
- CLAUDE.md toolkit rule #5 grows from four to five write-allowed
  exceptions; architectural rule #11 is added documenting the
  transit-line mirror.
- Three new POST endpoints (`/tools/compute_walkability`,
  `/tools/compute_amenity_supply`,
  `/tools/find_comparables_along_axis`), bearer-token-gated.

### Phase 7 slice 1: The reasoning agent (provider-agnostic)
Synchronous tool-use loop that takes a target spec + filters and
returns a defensible rental estimate by iterating over a curated
toolkit subset. Writes to `estimation_runs` with `mode='agent'`,
early-INSERTs `status='running'`, finalises to `success`/`failed`.
Trace records `kind='reasoning'` per LLM turn.
- **Provider-agnostic.** `api/providers/` defines a `CompletionProvider`
  Protocol with neutral message / tool / completion types; two
  implementations ship: `AnthropicProvider` (SDK = `anthropic`) and
  `GeminiProvider` (SDK = `google-genai`). `LLMClient` is now a
  provider-agnostic audit orchestrator. Adding a third provider is
  one new file implementing the same Protocol.
- **`skills` table + history trigger.** Each skill = a bundle of
  (system prompt + allowed tools + per-provider preferred model +
  loop limits). DB-backed at runtime; on-disk
  `skills/<name>/SKILL.md` is the canonical seed (committed in git
  as documentation). Operator edits live values via the Settings
  page; every change preserved in `skills_history`.
- **Curated tool subset for slice 1:**
  `find_comparables_relaxed`, `analyze_distribution`,
  `find_distribution_outliers`, `describe_neighborhood`,
  `verify_listing_freshness` + `record_estimate` terminator.
- **Settings page** (`/settings`) edits skills and `app_settings`.
  `/admin/*` routes are exempted from the `API_TOKEN` bearer gate
  per operator decision (private Railway URL is the security
  perimeter; same exemption category as `/health`).
- **Loop guards:** `max_iterations`, `max_cost_usd`,
  `wall_clock_timeout_s` — all sourced from the skill row, all
  short-circuit to `status='failed'` with `error_message`.
- **Migration 029** adds the `skills` + `skills_history` tables and
  trigger, the `'agent_estimation'` `called_for` enum, the
  `llm_calls.provider` column, and seeds `rental_estimator_v1`.
- Apartment rentals only (`byt` / `pronajem`). Multi-category
  defaults stay deferred to Phase 1.5b.

## Next

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
