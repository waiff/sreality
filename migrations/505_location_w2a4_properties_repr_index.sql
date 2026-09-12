-- 505_location_w2a4_properties_repr_index.sql
-- W2-a4 — the index the widened location sweep needs. PURELY ADDITIVE: one partial
-- index, no column, no constraint, no data change. Safe to re-run.
--
-- WHY. `browse_projection` serves `properties WHERE status = 'active'` — the MERGE
-- lifecycle, not `is_active` — so a DELISTED property is still served, with its
-- `repr_listing_ref_id` display listing carrying every place label Browse renders. The
-- location sweep (`location_data/resolver/drain.py::_SWEEP_SQL`) drove off `l.is_active`
-- alone, so that display listing could never get a `listing_location` row: after W3 its
-- Browse row shows no place and it drops off the map. The sweep's driving predicate
-- becomes
--
--     l.is_active
--       OR EXISTS (SELECT 1 FROM properties pr
--                   WHERE pr.repr_listing_ref_id = l.id AND pr.status = 'active')
--
-- and that EXISTS is CORRELATED (`pr.repr_listing_ref_id = l.id`) and sits under an OR,
-- which blocks the semi-join transform: Postgres evaluates it as a per-row subplan. So
-- it is one index probe per inactive listing with an index, and one SEQ SCAN OF
-- `properties` per inactive listing without one — on a statement that walks the whole
-- corpus in 250k-id windows. `properties` carried eleven indexes and NOT ONE led on
-- `repr_listing_ref_id` (091, 100, 142, 224, 251, 253), even though it is the column
-- every read model joins listings on (343, 363, 375, 398, 425, 475).
--
-- PARTIAL, deliberately. `status = 'active'` is exactly the predicate the sweep asks
-- (and the predicate `browse_projection` itself is defined by), so the index holds only
-- the rows that can ever match and stays a fraction of the table; `merged_away` rows are
-- never probed by this shape. `repr_listing_ref_id IS NOT NULL` drops the rows that
-- cannot match either — a property whose display listing was never stamped.
--
-- NOT CONCURRENTLY, for the reason recorded at length in migration 429: both Supabase MCP
-- paths wrap their payload in a transaction and `CREATE INDEX CONCURRENTLY` cannot run
-- inside one (25001), and a first attempt killed by the MCP's statement_timeout leaves an
-- INVALID index behind. A plain CREATE INDEX takes SHARE — it blocks WRITES on
-- `properties`, not reads, and this table's writers are the batch property-maintenance
-- jobs, which retry. The build is a partial index over ~640k rows: seconds, not minutes.
-- `lock_timeout` bounds the head-block (a queued ACCESS EXCLUSIVE would stall every
-- reader behind it); `statement_timeout` bounds the SHARE lock if the build stalls.
-- Prefer a quiet window between the */15 cron bursts, as 429 did.
--
-- Rollback: DROP INDEX CONCURRENTLY properties_repr_listing_ref_active_idx;
--   (out-of-band — it is only CREATE that the transaction wrapper blocks unrecoverably)

SET lock_timeout = '6s';
SET statement_timeout = '300s';

create index if not exists properties_repr_listing_ref_active_idx
  on public.properties (repr_listing_ref_id)
  where status = 'active' and repr_listing_ref_id is not null;
