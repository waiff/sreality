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

## UI track (parallel, independent of analytical phases)

A browser UI is now a recognized territory rather than a future "maybe."
This track runs in parallel with the analytical phases above; the
toolkit is what makes the UI worth building, but the UI doesn't gate
toolkit work.

### Phase U0: Foundation (done)
- `frontend/` placeholder folder with README declaring conventions.
- Migration 008 creates `*_public` views and grants `SELECT` to the
  `anon` role; sensitive columns (`raw_json`, `geom`, hashes, error
  messages) are never exposed.
- CLAUDE.md "Territories" section defines the boundary between the
  Python backend and the future frontend.

### Phase U1: Map MVP
Read-only map of active listings. Vite + React + TypeScript +
`supabase-js` against the `anon` key. Marker per listing from
`listings_public.lat/lng`; click opens a card with current price,
disposition, area, district. Hosting target TBD (Cloudflare Pages or
Vercel — picked when the work is opened).

### Phase U2: History view
Per-listing price-history sparkline from `listing_snapshots_public`,
plus a "verify freshness" button that calls the bearer-token-gated
FastAPI service.

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
