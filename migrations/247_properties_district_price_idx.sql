-- Browse cards/table time out (anon 3s statement_timeout) when a district is
-- selected AND the list is sorted by price. The planner walks the global
-- properties_price_keyset_idx (current_price_czk, id) ascending and applies the
-- district id + category + subtype as a heap Filter, so it scans every
-- globally-cheaper non-matching row before collecting the first page. Measured
-- on prod for "Praha kraj + dům/komerční + 5 subtypes + price low→high":
-- Rows Removed by Filter = 89,675, Execution Time = 9,191 ms (the red
-- "canceling statement due to statement timeout" the operator saw — the map
-- aggregate and the count, which don't ORDER BY price, returned fine).
--
-- Why it's price specifically: matching rows (houses/commercial in one kraj) are
-- concentrated at the EXPENSIVE end, a price↔category/subtype correlation the
-- planner can't model, so under a small LIMIT it always (mis)prefers the
-- price-ordered index. A district-leading index it would never choose
-- (verified: it reverts to the global price keyset). The only ordered index it
-- WILL pick is one led by (district, current_price_czk). To make that scan cheap
-- we carry category_main / subtype / is_active as trailing columns so they are
-- applied as ScalarArrayOp INDEX conditions (no heap fetch) instead of a heap
-- Filter. Same query then runs in ~70 ms (131x). `id` sits right after the price
-- so the (price, id) keyset ordering is native (no extra Sort) and page-2 cursor
-- predicates stay index-bounded. One btree serves both price directions
-- (DESC = backward scan).
--
-- One index per district level — a Browse district chip resolves to exactly one
-- of obec_id / okres_id / region_id (see districtsFilterClause). Partial
-- WHERE <col> IS NOT NULL mirrors the existing single-column district indexes
-- and stays usable for an equality predicate (= id implies NOT NULL).
--
-- NOTE: only the price sort was catastrophic; the other district + sort combos
-- (last_seen / first_seen / area) run ~1 s under budget, and the computed
-- price/m² sort is forced filter-first (no ordered index), so they are left as-is.
--
-- Prod (343k-row hot table, written every ~5 min) was built CONCURRENTLY out of
-- band; the statements here are plain + idempotent so a fresh rebuild (no rows)
-- and any replay are no-ops.

create index if not exists properties_region_price_filt_idx
  on properties (region_id, current_price_czk, id, category_main, subtype, is_active)
  where region_id is not null;

create index if not exists properties_okres_price_filt_idx
  on properties (okres_id, current_price_czk, id, category_main, subtype, is_active)
  where okres_id is not null;

create index if not exists properties_obec_price_filt_idx
  on properties (obec_id, current_price_czk, id, category_main, subtype, is_active)
  where obec_id is not null;
