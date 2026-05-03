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
- Image bytes mirrored to Cloudflare R2 (originally deferred; live since v1.5)
  so listings retain their photos after sreality's CDN expires them.

The schema is currently at migration 003. See [`migrations/`](./migrations)
for the full sequence; never modify a numbered file once it's been applied.

## Status

- [x] Schema applied (`migrations/001_initial.sql` through `003_listing_fetch_failures.sql`)
- [x] Scraper code (index walk, detail fetch, parse, upsert, snapshot-on-change)
- [x] CI workflows (`test.yml` per push, `scrape.yml` daily at 22:00 UTC)
- [x] Image mirroring to Cloudflare R2 with parallel uploads
- [x] Failure tracking (`listing_fetch_failures`) with priority retry
- [x] Conservative/aggressive run modes
- [x] First successful manual run (production)
