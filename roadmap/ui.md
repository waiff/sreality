# UI track

A browser UI is now a recognized territory rather than a future "maybe."
This track runs in parallel with the analytical phases; the toolkit
is what makes the UI worth building, but the UI doesn't gate toolkit
work.

## Done

### Phase U0: Foundation
- `frontend/` folder with README declaring conventions.
- Migration 008 creates `*_public` views and grants `SELECT` to the
  `anon` role; sensitive columns (`raw_json`, `geom`, hashes, error
  messages) are never exposed.
- CLAUDE.md "Territories" section defines the boundary between the
  Python backend and the future frontend.

### Phase U1a: Database browser
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
  inactive-only filter), 021 (`region_stats` `ppm2_box` extension).
- Browse-2 add-ons: `LocationSearchBox` + Mapy.cz suggest /
  resolve proxy (`/maps/suggest`, `/maps/resolve`),
  `DispositionBoxPlots` on the Region page.

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

### Phase estimation-4: Generic URL parser
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

### Phase U2: Estimation flow
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

### browse-2: Region search + box plots
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

### Phase estimation-5: URL-parser frontend
- `ConfidenceIndicator` component + per-field confidence surface on
  the review step.
- `previewListingUrl` + `useUrlPreview` React hook: drives the
  paste-URL step against `POST /estimations/preview`.
- Listing-block render on the URL step; `force_refresh` to bypass
  the 7-day cache; `cost_usd_total` rolled up from `llm_calls`.
- Commits `e9da41f`, `65b9967`, `d66da7e`. PR #29.

## Next

### Phase U2.5: Freshness write-path
"Verify freshness" button on Listing Detail that calls the
bearer-token-gated FastAPI service to refresh a listing on demand.
The audit log table (`listing_freshness_checks`) already exists from
Phase 2.5; the remaining work is the UI button + API call.

### Phase U3: Toolkit-backed views (later)
Surfacing `describe_neighborhood`, `find_distribution_outliers`, and
the velocity tools through the UI. Auth-gated; specific shape decided
when U1 + U2 are live.
