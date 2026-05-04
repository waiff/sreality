# Sreality rental tracker

Daily scraper for Czech rental listings from [sreality.cz](https://www.sreality.cz),
storing full history in Supabase Postgres (PostGIS) for downstream
rental-yield analysis.

The scraper runs as a scheduled GitHub Action; there is no local development
requirement. See [`CLAUDE.md`](./CLAUDE.md) for architectural rules and
operational notes.

## Layout

```
migrations/         numbered SQL migrations (applied via Supabase MCP after approval; tracked in this folder as the source of truth)
scraper/            Python package: HTTP client, parser, DB writer, entrypoint
toolkit/            pure-function analytical tools over the schema
api/                FastAPI service exposing the toolkit (deployed to Railway)
frontend/           reserved for the future browser UI (placeholder; see frontend/README.md)
tests/              pytest suite
.github/workflows/  test.yml (per-push) and scrape.yml (daily cron)
```

## Scope

- Apartments, rentals, all of Czech Republic.
- Two-phase scrape: index pages, then per-listing detail.
- Upsert into `listings`; append a row to `listing_snapshots` only when the
  content hash changes; mark unseen listings `is_active=false`.
- Image bytes mirrored to Cloudflare R2.
- Analytical toolkit (`find_comparables`, `analyze_distribution`,
  `verify_listing_freshness`, `compare_snapshots`) exposed as a FastAPI
  service with a composite `/estimate_yield` endpoint. Bearer-token
  gated via `API_TOKEN`.

## Status

- [x] Schema applied (migrations 001–008)
- [x] Scraper code
- [x] CI workflows (test on push, daily cron)
- [x] Image mirroring to R2 live
- [x] Failed-fetch tracking with give-up threshold
- [x] Locality IDs promoted to typed columns
- [x] Toolkit + FastAPI service deployed to Railway
- [x] Freshness layer (`verify_listing_freshness`, `compare_snapshots`,
      `listing_freshness_checks` audit table)
- [x] API auth (`API_TOKEN` bearer-token gate)
- [x] `describe_neighborhood` (dispositional/price/condition profile with trend)
- [x] `find_distribution_outliers` (outlier detection with cross-referenced reasons)
- [x] `compute_market_velocity` (TOM stats and trend for a filtered cohort)
- [x] `compute_listing_velocity` (per-listing TOM percentile and fast/typical/slow/stuck classification)
- [x] UI foundation: `*_public` read views with `anon`-role grants; `frontend/` territory placeholder (no UI ships yet)

See [`ROADMAP.md`](./ROADMAP.md) for the long-term plan.
