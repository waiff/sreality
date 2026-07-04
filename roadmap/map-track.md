> Track file — part of [ROADMAP.md](../ROADMAP.md). After shipping, edit only this file + its index row.

## Map track (parallel)

Geographic drill-down beyond the existing district facet. Independent
of the analytical and UI phases; runs alongside them.

### map-1: typed locality IDs
- **Part A (done):** inspection of `raw_json.recommendations_data`
  confirmed 100% coverage on `locality_municipality_id`,
  `locality_quarter_id`, and `locality_ward_id` across active
  listings. Cardinality and naming notes captured in commit
  `d663233`.
- **Part B (proposed):** migration 016 promotes those three IDs to
  typed columns, sanitising sreality's `-1` sentinel to `NULL`.
  Parser + scraper write-path landed in commit `d663233`; migration
  is committed in `migrations/016_locality_ids_extended.sql`.
  Confirm-and-mark-done item: verify whether Part B has actually
  been applied to the production database (auto-status block above
  should show migration 016 applied if so) and update this entry
  accordingly.
- **Part C (done):** backfill from `raw_json` for existing rows;
  exposed via `listings_public`.
- **Part D (done):** spatial join scaffolding for ČÚZK / RÚIAN
  polygons. Migration 017 (`admin_boundaries`),
  `scripts/ingest_boundaries.py`,
  `.github/workflows/ingest_boundaries.yml`. Bridge table populated
  by the ingest workflow.

