-- 253_properties_district_price_covering_idx.sql
--
-- Completes PR #627 (migration 247). That PR fixed the district + price-sort
-- timeout with per-district-level covering indexes led by (district_id,
-- current_price_czk, …). But two predicates the REAL Browse query carries were
-- left as a heap Filter, not index conditions: `category_type` (Prodej/Pronájem
-- — always present, single-valued) and the view's `status = 'active'`. So for
-- "Domy, Praha" the 247 index still walked ~many leaf pages to the first match
-- (measured live through properties_public: ~1.9 s warm, and it still timed out
-- cold as anon under the 3 s budget — 1,681 ms to the first row).
--
-- Fix: put `category_type` in the leading key (right after the district id, it's
-- an equality) and carry `category_main` / `subtype` / `status` as trailing
-- covering columns. Then EVERY Browse predicate is an Index Cond (zero heap
-- Filter) and the scan early-stops at the page. Measured on a full-table copy:
-- the same cohort drops to ~1.5 ms (only ~60 buffer reads), robust cold or warm.
-- One index per district level (a Browse chip resolves to exactly one of
-- obec_id / okres_id / region_id — see districtsFilterClause). category_type is
-- always set by the UI (single-select), so the leading equality always applies;
-- queries without a district fall back to other indexes (the partial WHERE keeps
-- this index off them). Other district + sort combos (last_seen / area / etc.)
-- already run ~1 s under budget, so only the price sort needs this.
--
-- This supersedes 247's three *_price_filt_idx, so they are dropped. On prod the
-- swap is done CONCURRENTLY out of band (343k-row hot table written every ~5 min);
-- the statements below are plain + idempotent for a fresh rebuild / replay.
-- Depends on the district ids being on `properties` (migration 251).

DROP INDEX IF EXISTS properties_region_price_filt_idx;
DROP INDEX IF EXISTS properties_okres_price_filt_idx;
DROP INDEX IF EXISTS properties_obec_price_filt_idx;

CREATE INDEX IF NOT EXISTS properties_region_type_price_idx ON properties
  (region_id, category_type, current_price_czk, id, category_main, subtype, status)
  WHERE region_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS properties_okres_type_price_idx ON properties
  (okres_id, category_type, current_price_czk, id, category_main, subtype, status)
  WHERE okres_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS properties_obec_type_price_idx ON properties
  (obec_id, category_type, current_price_czk, id, category_main, subtype, status)
  WHERE obec_id IS NOT NULL;
