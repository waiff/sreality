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

## Next

### Phase 4: Spatial context (external data)
Tenant-perspective overlays beyond what's in the listings table.
- `find_anchor_amenities`: OSM/Mapy.cz POI lookup.
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

## Out of scope until explicitly opened
- Frontend (Lovable map UI).
- ClickUp integration.
- MCP server wrapping the toolkit (for ad-hoc chat with the data).
- Public read API beyond the bearer-token gate.

## Data preconditions
- Velocity tools (Phase 3b) work today (1 snapshot per listing is enough
  for TOM math).
- Outlier history-pattern detection (Phase 3a) becomes more useful as
  snapshot density grows past ~1.5/listing average.
- Cluster detection (Phase 5) needs neighborhoods with 30+ comparables
  to be meaningful; sparse rural areas will return single-cluster
  results.
