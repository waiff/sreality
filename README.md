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
tests/              pytest suite
.github/workflows/  test.yml (per-push) and scrape.yml (daily cron)
```

## v1 scope

- Apartments, rentals, all of Czech Republic.
- Two-phase scrape: index pages, then per-listing detail.
- Upsert into `listings`; append a row to `listing_snapshots` only when the
  content hash changes; mark unseen listings `is_active=false`.
- Image bytes mirrored to Cloudflare R2 (originally deferred; now live).

## Status

- [x] Schema applied (migrations 001–004)
- [x] Scraper code
- [x] CI workflows (test on push, daily cron)
- [x] First successful manual run
- [x] Image mirroring to R2 live
- [x] Failed-fetch tracking with give-up threshold
- [x] Locality IDs promoted to typed columns
