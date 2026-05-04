# Roadmap

The long-term plan for this project. Each phase builds on the previous;
tools within a phase are independent. CLAUDE.md is the authoritative
source for active rules; ROADMAP is for sequencing.

## Done

### Phase 1: Scraper
Daily index + on-demand detail scrape of sreality.cz. Image mirroring to
Cloudflare R2. Failure tracking with give-up threshold. Two-mode GitHub
Actions workflow (conservative cron, opt-in aggressive bootstrap).

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

### Phase U1b: Estimation backend
- `estimation_runs` table (migration 010): persistent record of every
  estimation, regardless of trigger. Schema reserves `mode='agent'`
  and `status='pending'/'running'` for U4 without forcing today's
  code to write twice.
- `scraper.url_parser`: turns a sreality URL into a parsed spec by
  reusing `scraper.parser`.
- `/estimations` endpoints: POST creates a run (URL or spec), GET-by-id
  reads one, GET lists with filters and pagination.
- Trace format v1: tool calls + computations recorded with
  `output_summary` only (full data in dedicated columns).

### Phase 4a: Spatial context — anchor amenities (done)
- `find_anchor_amenities`: OSM POI lookup with local cache mirror in
  the `amenities` + `amenity_fetches` tables (cache-key = category +
  radius + center + TTL). Live behind the API; one of the two
  toolkit write-allowed exceptions per CLAUDE.md.

## Next

### Phase 4b: Spatial context (remaining)
Tenant-perspective overlays beyond anchor amenities.
- `compute_walkability`: scored composite of POI distances.
- `find_comparables_along_axis`: transit-line-aware comparable search.

### Phase 5: Statistical refinement
- `cluster_comparables`: k-means on cohorts to surface sub-markets.
- `find_comparables_relaxed`: auto-widening with provenance when strict
  filters return too few results.

### Phase 6: Visual layer
- `summarize_listing`: structured Claude API summary of a raw listing.
- `compare_listing_images`: pairwise visual similarity via Claude vision.

### Phase 7: The reasoning agent
Composes the validated toolkit. Built only after the toolkit has been
used and refined for ~1 month against real data.

## UI track (parallel, independent of analytical phases)

A browser UI is now a recognized territory rather than a future "maybe."
This track runs in parallel with the analytical phases above; the
toolkit is what makes the UI worth building, but the UI doesn't gate
toolkit work.

### Phase U0: Foundation (done)
- `frontend/` folder with README declaring conventions.
- Migration 008 creates `*_public` views and grants `SELECT` to the
  `anon` role; sensitive columns (`raw_json`, `geom`, hashes, error
  messages) are never exposed.
- CLAUDE.md "Territories" section defines the boundary between the
  Python backend and the future frontend.

### Phase U1a: Database browser (done)
Read-only Vite + React + TS SPA over the `*_public` views with the
`anon` key. Deployed to Railway as a second service alongside the
FastAPI backend. Civic-archive visual direction (laid-paper canvas,
oxidised-copper accent, Fraunces / Inter / JetBrains Mono, tabular
numerals, Czech locale formatting).
- **Browse**: filter sidebar (district typeahead, disposition multi-toggle,
  dual-handle price + area sliders, tri-state status, last-seen-within,
  has-balcony/lift/parking) → Map / Table / Stats tabs. Filter and
  sort state in URL params; bookmarkable, refresh-survives.
- **Listing detail** (`/listing/:sreality_id`): hero, mini-map, key
  facts, snapshot timeline strip (the product's signature visual
  vocabulary), per-snapshot diff table, freshness check log,
  outbound link to sreality.cz.
- **Region**: district multiselect or radius-from-pin definition; live
  aggregates (count, p25/median/p75 price + price/m², per-disposition
  median table), 90-day active-per-day chart, 12-week new-listings bar,
  median time-on-market for delisted listings.
- **Health**: operator dashboard. Last-scrape recency (with
  36-hour stale banner), active count + Δ vs 7 days ago, new-listings
  14-day chart, snapshot-density buckets, freshness checks 24h by
  outcome, fetch-failures table.
- Migrations 011 (`browse_stats`), 012 (`region_stats` +
  `region_active_by_day`), 013 (`health_summary`), 014 (`browse_stats`
  inactive-only filter).

### Phase U1b: Estimation backend (done)
- `estimation_runs` table (migration 010): persistent record of every
  estimation, regardless of trigger. Schema reserves `mode='agent'`
  and `status='pending'/'running'` for U4 without forcing today's
  code to write twice.
- `scraper.url_parser`: turns a sreality URL into a parsed spec by
  reusing `scraper.parser`.
- `/estimations` endpoints: POST creates a run (URL or spec), GET-by-id
  reads one, GET lists with filters and pagination.
- Trace format v1: tool calls + computations recorded with
  `output_summary` only (full data in dedicated columns).

### Phase U2: Estimation flow (done)
End-to-end browser flow over the U1b backend.
- `/estimate`: two-step form (paste URL or pick listing → review and
  edit spec → submit). Pre-fills from `/estimations/preview`; on
  submit POSTs `CreateEstimationIn` to the FastAPI service. URL-origin
  runs send `url` + a minimal `spec_overrides` diff so the server
  records the original `input_url` for traceability.
- `/estimations`: list view of past runs with source/status filters,
  URL-state-driven pagination, links to detail.
- `/estimation/:id`: complete display — rent range strip,
  confidence/source pills, warnings block (failed runs render
  `error_message` and a truncated trace, no range), input recap,
  trace timeline, comparables table sorted by data age, re-run
  button (POSTs new run with `parent_run_id` set).
- `Timeline` component: dispatches on `step.kind` via a renderer
  map (`tool_call` / `computation` / `reasoning`). Today renders
  the deterministic 4-step trace; the same component will render
  the U4 agent's longer traces without rework. Smart default
  expansion (last step + steps over 500 ms).

### Phase U2.5: Freshness write-path (next)
"Verify freshness" button on Listing Detail that calls the
bearer-token-gated FastAPI service to refresh a listing on demand.
The audit log table (`listing_freshness_checks`) already exists from
Phase 2.5; the remaining work is the UI button + API call.

### Phase U3: Toolkit-backed views
Surfacing `describe_neighborhood`, `find_distribution_outliers`, and
the velocity tools through the UI. Auth-gated; specific shape decided
when U1 + U2 are live.

## Out of scope until explicitly opened
- ClickUp integration.
- MCP server wrapping the toolkit (for ad-hoc chat with the data).
- Public read API beyond the bearer-token gate.
- Per-user identity / accounts in the UI (the `anon` key is shared and
  read-only; the FastAPI token is shared and gated).

## Data preconditions
- Velocity tools (Phase 3b) work today (1 snapshot per listing is enough
  for TOM math).
- Outlier history-pattern detection (Phase 3a) becomes more useful as
  snapshot density grows past ~1.5/listing average.
- Cluster detection (Phase 5) needs neighborhoods with 30+ comparables
  to be meaningful; sparse rural areas will return single-cluster
  results.
