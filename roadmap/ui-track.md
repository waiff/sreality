> Track file — part of [ROADMAP.md](../ROADMAP.md). After shipping, edit only this file + its index row.

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
  outcome, fetch-failures table. Per-scrape audit added in
  migration 086 (`scrape_runs` + `recent_scrape_runs` /
  `image_storage_overview` RPCs) — Recharts time-series of
  scraped-new / inactive / images-stored over the last 14 days,
  expandable per-run table broken out by category pair, image-mirror
  progress (stored / total), and a static cron schedule card.
- Migrations 011 (`browse_stats`), 012 (`region_stats` +
  `region_active_by_day`), 013 (`health_summary`), 014 (`browse_stats`
  inactive-only filter), 021 (`region_stats` `ppm2_box` extension).
- Browse-2 add-ons (done): `LocationSearchBox` + Mapy.cz suggest /
  resolve proxy (`/maps/suggest`, `/maps/resolve`),
  `DispositionBoxPlots` on the Region page.

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

### Phase estimation-4: Generic URL parser (done)
- Migration 020: `parsed_url_cache`, `llm_calls`, `app_settings` +
  `app_settings_history`, plus `source_kind` /
  `parse_confidence` / `parse_confidence_per_field` / `source_html`
  columns on `estimation_runs`.
- `api.llm_client.LLMClient`: wraps the Anthropic SDK, audits every
  call to `llm_calls`, computes USD cost from a per-model price
  table, and emits a one-time daily-cost soft warning at
  `LLM_DAILY_COST_WARN_USD` (default $5).
- `scraper.geocoding.geocode`: Mapy.cz forward geocoding with
  type-based confidence (regional.address → high; street → medium;
  city centroid → low) and a CLI verification helper.
- `scraper.source_dispatcher`: classifies a URL by domain and routes
  to either the deterministic sreality flow or the LLM-driven
  per-source parsers (bezrealitky, idnes_reality, remax,
  best-effort generic). Cache lookup on canonicalised URL hash with
  7-day TTL.
- `POST /estimations/preview`: parse any allowlisted URL through the
  LLM-driven dispatcher and return spec + provenance without
  persisting a run. Coexists with the U2-frontend's existing
  `GET /estimations/preview` (sreality-only, read-only); the POST
  version is the path forward for non-sreality sources.
- `POST /estimations`: now routes through the dispatcher and
  populates the four new audit columns; parse failures persist a
  `failed` row with the error message.

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

### browse-2: Region search + box plots (done)
- Mapy.cz suggest / resolve proxy endpoints (`api/maps.py`) — bearer-
  gated, 5-min in-process TTL cache on suggest, admin_boundaries-aware
  polygon resolution that auto-degrades to point + radius when the
  table is missing or empty.
- Region page rebuilt around a single `LocationSearchBox`: typing a
  street address, neighbourhood, or kraj returns ranked Mapy.cz
  suggestions and resolves to either a polygon (when admin_boundaries
  ships) or a point + radius. Browse-1's district / radius pickers
  remain reachable under an "Advanced" disclosure for legacy
  bookmarks and direct radius drag-and-drop.
- Per-disposition price-per-m² box plots (custom SVG) replace
  browse-1's median-only summary table. Tukey 1.5×IQR whiskers
  clipped to min/max, copper median line, no outlier dots, no
  per-disposition colour-coding. A numeric table beneath the SVG
  preserves precise readouts.
- Migration 021 (`021_region_stats_box.sql`) extends `region_stats`
  with a per-disposition `ppm2_box` field; existing fields preserved
  for backwards compatibility.

### Phase estimation-5: URL-parser frontend (done)
- `ConfidenceIndicator` component + per-field confidence surface on
  the review step.
- `previewListingUrl` + `useUrlPreview` React hook: drives the
  paste-URL step against `POST /estimations/preview`.
- Listing-block render on the URL step; `force_refresh` to bypass
  the 7-day cache; `cost_usd_total` rolled up from `llm_calls`.
- Commits `e9da41f`, `65b9967`, `d66da7e`. PR #29.

### Phase U-BV: Browse velocity, card badges, filter overhaul (done)
- Migration 052 promotes "turned in" (TOM = days on market) to a
  first-class column on `listings_public`. Same definition as
  `toolkit/velocity._tom_days`: `now() - first_seen_at` for active
  rows, `last_seen_at - first_seen_at` for delisted. SQL and Python
  now share one authoritative computation.
- Migration 053 redoes `browse_stats` with a new filter surface:
  `tom_days_min/max`, `last_seen_min/max_days` and
  `first_seen_min/max_days` (both replacing the old preset
  `seen_within_days_filter`), `building_type_filter text[]`.
  Implicit `active_only=true` default dropped — Browse no longer
  hides delisted listings unless asked.
- Toolkit `ComparableFilters` grows the same six filter fields
  (`tom_days_min/max`, `last_seen_min/max_days`,
  `first_seen_min/max_days`) and flips defaults so no implicit
  freshness gate fires. The deterministic estimator's
  `_DEFAULT_ACTIVE_ONLY` and per-kind `max_age_days` are gone with
  it. Velocity logic is unchanged; the new filter fields flow
  through `_shared_filter_where` for free.
- API: `FindComparablesIn`, `EstimateYieldIn`,
  `ComputeMarketVelocityIn`, `DescribeNeighborhoodIn`, and
  `CreateEstimationIn` all grow the six new optional filters; the
  deterministic `_build_filters` plumbs them through. Agent's
  `base_filters` carry them per-run without per-tool schema bloat.
- Frontend: `ListingFilters` adds `tomDaysMin/Max`,
  `lastSeenMinDays/MaxDays`, `firstSeenMinDays/MaxDays`,
  `buildingMaterial`. `applyFilters` plumbs the days-ago ranges
  against `last_seen_at` / `first_seen_at` and the TOM range against
  `tom_days`. The four-bucket Building material picker (Cihla /
  Panel / Smíšená / Ostatní) maps "Ostatní" to the five remaining
  sreality values. Default `status` is now `'any'`.
- Filter panel regrouped: Category / Location / Disposition / Price /
  Size / Status & velocity / Building / Amenities / Curation.
  ControlGroup legend bumped (0.82rem, ink-primary, semibold) so it
  visually outweighs the smaller Section labels (0.62rem,
  ink-tertiary). Redundant inner labels dropped on singleton groups.
- Browse cards now stack four metadata badges down the right margin:
  status (sage `Aktivní` / brick `Neaktivní`), first-seen (`od 5. 5.`),
  last-seen (`viděno 8. 5.`), and the copper TOM pill
  (`94 dní`, Czech plural). Re-uses the existing token palette and
  borders-only depth strategy; no new design tokens.
- Migration 061 enriches `browse_stats` with a
  `price_quartile_velocity` field: the filtered cohort is split into
  four equal-size price buckets via `ntile(4)` and each bucket reports
  its `tom_days` distribution alongside its price range. Stacks on
  top of 060's expanded signature — DROP-then-CREATE because the
  function body grows a new CTE; the parameter list is unchanged from
  060. The Stats tab renders this as a fourth Card ("Turnover by
  price quartile") with horizontal box plots reusing the
  `DispositionBoxPlots` SVG idiom. Active vs. delisted semantics of
  the per-bucket TOM follow the user's status filter — no per-bucket
  active/inactive split is computed.
- Migration 062 adds `mean` to each bucket's `tom_box` so the
  price/velocity signal isn't lost when `tom_days` is integer-clumped.
  With a 14-day scrape window the five-number summary collapses to
  identical medians across all four buckets even though means differ
  monotonically (active 2+kk byt/pronajem: 8.9 / 9.0 / 9.4 / 9.9 days).
  Frontend renders the mean as a copper dot on the box plot and a new
  MEAN column in the numeric table; the caption now names the
  integer-flooring caveat explicitly.
- Migration 063 replaces the four-equal-bucket
  `price_quartile_velocity` with a seven-band percentile split
  `price_band_velocity`: p0–p10, p10–p25, p25–p45, p45–p55, p55–p75,
  p75–p90, p90–p100. Narrower bands at the tails and around the
  median, wider through the body, so the chart surfaces tail-vs-body
  differences that an equal-quartile split would mask. The new
  payload also reports `pct_share` per band (actual share of priced
  cohort, since ties at percentile cuts make bucket sizes drift from
  their nominal 10/15/20/10/20/15/10). Active 2+kk byt/pronajem shows
  the body bands clustered at mean ≈ 9.2d while the priciest decile
  jumps to 10.5d. Frontend rewrite: `PriceQuartileVelocity` →
  `PriceBandVelocity`, seven rows on the y-axis with percentile +
  price-range + n + share labels; Card heading and caption updated
  accordingly.

### Phase U2.5: Freshness write-path (done)
- "Ověřit aktuálnost" (Verify freshness) button on Listing Detail's
  freshness-checks section. Calls the bearer-token-gated
  `POST /tools/verify_listing_freshness` on demand via the existing
  `request()` auth path (no new auth mechanism) — `max_age_hours: 0`
  so an operator click always forces a real re-fetch rather than the
  throttle's `cached` short-circuit.
- `verifyListingFreshness` wrapper + `VerifyFreshnessResult` /
  `FreshnessOutcome` types in `frontend/src/lib/api.ts`.
- Pending state ("Ověřuji…" / "Re-fetching the listing from the
  source…") and a result line that maps the outcome
  (`unchanged` / `updated` / `gone` / `fetch_error` / `cached`) to a
  human message + the existing `OutcomeChip`. On success it
  invalidates the `listing`, `snapshots`, and `freshness` queries so
  the timeline strip and the check log refetch immediately.
- The audit log table (`listing_freshness_checks`) and the wrapped
  `scraper.freshness.freshness_check` already existed from Phase 2.5;
  this phase added only the frontend affordance. Backed by a
  `FreshnessBlock` component test (the full live e2e needs production
  secrets).

### Phase U-RM: Unified Browse read model (done)
- Implements `docs/design/browse-read-model.md` (PRs #707/#708 aftermath;
  operator decisions 2026-07-07: default sort → `first_seen_at DESC`,
  list freshness 5 min).
- `browse_projection` (migration 276) — the ONE place the Browse column
  contract + publication-gate predicate live. `browse_list` (UNLOGGED
  snapshot, every active property incl. coordinate-less; lean 5-index
  set) + `properties_map_mv` both rebuild FROM it via SECURITY DEFINER
  functions on pg_cron (`*/5` list, `7,37` map — migration 277);
  `scripts/refresh_map_mv.py` + its workflow retired. Rebuild stamps in
  `browse_read_model_state(_public)`.
- Frontend cards/table/count/no-price re-pointed to `browse_list`;
  `fetchBrowseCount` flipped to EXACT-FIRST (index-only ~200 ms cold
  market-wide; `count=planned` is the fallback, "~N" now rare);
  `DEFAULT_SORT` → `first_seen_at DESC`. Stats RPC re-pointed
  (migration 278, body-only) — cards/table/count/stats read ONE
  snapshot and cannot disagree.
- Guardrails: gate-wrap pinned on `browse_projection`; rebuild
  invariants (ANALYZE-before-swap, pg_notify); read-contract test
  (registry + CARD/TABLE/MAP_COLS + SORTABLE_FIELDS ⊆ projection).
- Deferred: live-table index retirement after a ≥7-day
  pg_stat_user_indexes observation window (operator OK required);
  row-comparison keyset cursor (PostgREST can't emit it yet).

### Phase U-Nav: Unified browse → detail navigation (next)

Today the top nav exposes `Listing` and (historically) `Estimate` as
top-level destinations. That conflates two distinct UX roles:
**list pages** (Browse, Estimations, Collections) are entry points,
**detail pages** (Listing, Estimation, Building, Collection item) are
drill-downs from those lists. The standalone `/listing` entry with no
id resolves to an empty shell and the entry only exists to satisfy
the menu link — a tell that the IA is wrong. This phase collapses
detail pages back into their parent flows and adds an explicit
"where am I, how do I get back" affordance.

**Scope:**
- **Remove from menu:** `Listing` link (currently `frontend/src/components/Shell.tsx:10`)
  and the `path: 'listing'` (no-id) route in `frontend/src/routes.tsx`.
  `Estimate` is already gone — the modal-trigger CTA in the top bar
  replaces it. Menu becomes: Browse, Region, Estimations, Collections,
  Health.
- **Detail pages stay reachable only via drill-down** from their
  parent list. `/listing/:sreality_id`, `/estimation/:id`,
  `/building/:id`, `/collection/:id` are unchanged as URLs; they
  just no longer have a nav entry.
- **Breadcrumbs + back affordance** on every detail page. Renders the
  parent context (e.g. "Browse / Praha 6 — 2+kk apartments / Listing
  detail") and preserves the parent's filter/sort/page state on
  click. Mechanism: when a list-page link navigates to a detail, it
  stashes the current URL (with all query params) into router
  state; the breadcrumb's "back" link reads from that state and
  falls back to the bare list URL if state is missing
  (deep-link / refresh case).
- **URL hierarchy** — pick one of the patterns below during the
  design kickoff. Not a foregone conclusion which is right for this
  app; documenting the trade-offs so the operator can choose.

**Proposed UX patterns (to discuss before any code lands):**

1. **Breadcrumb + flat URLs (recommended starting point).**
   Keep today's flat routes (`/browse`, `/listing/:id`) and add a
   breadcrumb strip + a sticky "← Back to results" link that
   restores the parent's filter state from router state. Pattern
   used by Airbnb, Zillow, GitHub's issue/PR detail pages.
   Cheap to ship, no migration of bookmarked URLs, breadcrumb
   degrades gracefully on deep-link / refresh (still shows
   "Browse" as parent, just without the specific filter context).
2. **Nested URLs reflecting drill-down.**
   `/browse/listing/:sreality_id`, `/estimations/:id`. The URL is
   the breadcrumb — back button = strip last segment. Pattern used
   by Linear, Notion, most file-tree UIs. Stronger sense of
   hierarchy; downside is the same listing reached from Collections
   would live at `/collections/:cid/listing/:sreality_id`, forcing
   either route duplication or a canonical-detail-URL convention.
   Bookmark migration needed (redirects from `/listing/:id`).
3. **Detail-as-overlay (modal / side sheet).**
   Clicking a listing in the Browse table opens an overlay over the
   filtered list rather than navigating away. Pattern used by Gmail,
   Linear's command-K previews, Booking.com's room picker. Best
   when users browse → preview → browse repeatedly. Trade-off: the
   detail loses real-estate, deep-linking requires a parallel
   full-page route anyway, and the snapshot timeline (the product's
   signature element) wants the full page.

Realistic combo: pattern 1 across the board, with pattern 3 as a
future enhancement on Browse → Listing once the breadcrumb is in
place and we know which detail interactions stay shallow.

**Out of scope for this phase:** changing the snapshot timeline,
adding new detail-page content, restyling list pages. Pure IA +
navigation work.

### Phase U3: Toolkit-backed views (later)
Surfacing `describe_neighborhood`, `find_distribution_outliers`, and
the velocity tools through the UI. Auth-gated; specific shape decided
when U1 + U2 are live.

