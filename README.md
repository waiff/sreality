# Sreality rental tracker

Daily scraper for Czech rental listings from [sreality.cz](https://www.sreality.cz),
storing full history in Supabase Postgres (PostGIS) for downstream
rental-yield analysis. A read-only browser UI sits over the same database
for QA and ad-hoc research.

The scraper runs as a scheduled GitHub Action; the API and the UI run as
two Railway services. There is no local development requirement. See
[`CLAUDE.md`](./CLAUDE.md) for architectural rules and operational notes.

## Layout

```
migrations/         numbered SQL migrations (applied via Supabase MCP after approval; tracked in this folder as the source of truth)
scraper/            Python package: HTTP client, parser, DB writer, entrypoint
toolkit/            pure-function analytical tools over the schema
api/                FastAPI service exposing the toolkit (deployed to Railway)
frontend/           Vite + React + TS database-browser UI (deployed to Railway as a second service; see frontend/README.md)
tests/              pytest suite
.github/workflows/  test.yml (per-push), scrape.yml (daily cron), frontend-build.yml (typecheck + bundle-size guardrail)
```

## Scope

- Apartments, rentals, all of Czech Republic.
- Two-phase scrape: index pages, then per-listing detail.
- Upsert into `listings`; append a row to `listing_snapshots` only when the
  content hash changes; mark unseen listings `is_active=false`.
- Image bytes mirrored to Cloudflare R2 (originally deferred; live since v1.5)
  so listings retain their photos after sreality's CDN expires them.
- Analytical toolkit (`find_comparables`, `analyze_distribution`,
  `verify_listing_freshness`, `compare_snapshots`) exposed as a FastAPI
  service with a composite `/estimate_yield` endpoint. Bearer-token
  gated via `API_TOKEN`.
- Browser UI (`frontend/`) reads directly from the `*_public` views with
  the Supabase anon key. Four pages: Browse (filters + map / table /
  stats), Listing detail (with snapshot timeline), Region (district or
  radius aggregates), Health (scraper-health dashboard).

## Status

- [x] Schema applied (migrations 001–014)
- [x] Scraper code (index walk, detail fetch, parse, upsert, snapshot-on-change)
- [x] CI workflows (`test.yml` per push, `scrape.yml` daily at 22:00 UTC, frontend build)
- [x] Image mirroring to Cloudflare R2 with parallel uploads
- [x] Failure tracking (`listing_fetch_failures`) with priority retry and give-up threshold
- [x] Conservative/aggressive run modes
- [x] Locality IDs promoted to typed columns
- [x] Toolkit + FastAPI service deployed to Railway
- [x] Freshness layer (`verify_listing_freshness`, `compare_snapshots`,
      `listing_freshness_checks` audit table)
- [x] API auth (`API_TOKEN` bearer-token gate)
- [x] `describe_neighborhood` (dispositional/price/condition profile with trend)
- [x] `find_distribution_outliers` (outlier detection with cross-referenced reasons)
- [x] `compute_market_velocity` (TOM stats and trend for a filtered cohort)
- [x] `compute_listing_velocity` (per-listing TOM percentile and fast/typical/slow/stuck classification)
- [x] Estimation runs persisted to `estimation_runs` table; surfaced via `/estimations` endpoints (POST/GET-by-id/list)
- [x] UI foundation: `*_public` read views with `anon`-role grants
- [x] **U1a database browser**: Browse / Listing / Region / Health pages; migrations 011–014 (`browse_stats`, `region_stats`, `health_summary`); deployed to Railway as a second service
- [x] **estimation-4 generic URL parser**: source-kind dispatcher routes any listing URL through either the deterministic sreality flow or LLM-driven per-source parsers (bezrealitky, reality.idnes, remax, best-effort generic). 7-day URL→spec cache, Mapy.cz geocoding, per-call cost audit in `llm_calls`, daily soft-warn at $5. New `/estimations/preview` endpoint returns the parsed spec without creating a run; `POST /estimations` populates `source_kind` / `parse_confidence` / `parse_confidence_per_field` / `source_html`.

See [`ROADMAP.md`](./ROADMAP.md) for the long-term plan.

## Frontend

A read-only web UI over the Supabase database, deployed to Railway alongside
the API as a separate service. Reads exclusively via the `*_public` views
with the `anon` key — no service-role credential ever ships to the browser.

Setup notes for developers in [`frontend/README.md`](./frontend/README.md).
The operator should ask Claude for the deployed URL.
