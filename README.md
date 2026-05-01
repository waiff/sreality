# Sreality rental tracker

Daily scraper for Czech rental listings from [sreality.cz](https://www.sreality.cz),
storing full history in Supabase Postgres (PostGIS) for downstream
rental-yield analysis.

The scraper runs as a scheduled GitHub Action; there is no local development
requirement. See [`CLAUDE.md`](./CLAUDE.md) for architectural rules and
operational notes.

## Layout

```
migrations/         numbered SQL migrations (run by hand in Supabase SQL editor)
scraper/            Python package: HTTP client, parser, DB writer, entrypoint
tests/              pytest suite
.github/workflows/  test.yml (per-push) and scrape.yml (daily cron)
```

## v1 scope

- Apartments, rentals, all of Czech Republic.
- Two-phase scrape: index pages, then per-listing detail.
- Upsert into `listings`; append a row to `listing_snapshots` only when the
  content hash changes; mark unseen listings `is_active=false`.
- Image URLs only - no file downloads.

## Status

- [x] Schema applied (`migrations/001_initial.sql`)
- [ ] Scraper code
- [ ] CI workflows
- [ ] First successful manual run
