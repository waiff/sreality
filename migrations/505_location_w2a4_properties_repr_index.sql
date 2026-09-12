-- 505_location_w2a4_properties_repr_index.sql
-- W2-a4 — the index the widened location sweep needs. PURELY ADDITIVE: one partial
-- index, no column, no constraint, no data change. Safe to re-run.
--
-- APPLY PATH: `apply_migration.yml`, NOT the Supabase MCP. This file contains
-- CREATE INDEX CONCURRENTLY, which cannot run inside a transaction block (25001), and
-- every MCP path (`execute_sql`, `apply_migration`) wraps its payload in one. The
-- workflow runs `psql -X -q -v ON_ERROR_STOP=1 -f <file>` with deliberately NO
-- --single-transaction, so statements autocommit and CONCURRENTLY is legal — that is
-- the workflow's stated contract. The CI schema replay (`migrations.yml`) applies each
-- file the same way, `psql -v ON_ERROR_STOP=1 -q -f "$f"`, so the replay and the real
-- apply agree. There is NO `begin`/`commit` in this file, and there must not be.
-- Timeouts are therefore plain `SET`, not `SET LOCAL` — outside a transaction
-- SET LOCAL is a silent no-op.
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
-- CONCURRENTLY, and this one is not optional. `properties` is written EVERY MINUTE by
-- the scrapers and by property maintenance, and the instance is IO-bound right now
-- (every active backend on DataFileRead). A plain CREATE INDEX takes SHARE, which blocks
-- those writers for the whole build — minutes, under this IO. CONCURRENTLY takes only
-- SHARE UPDATE EXCLUSIVE: it costs two table scans and a wait for in-flight
-- transactions, and writers keep running throughout. Migration 429 built its index the
-- blocking way and recorded why it had to (the MCP was the only apply path then); the
-- `apply_migration.yml` lane exists precisely so that trade is no longer forced.
--
-- THE INVALID-INDEX GUARD. A CONCURRENTLY build that fails — a lock timeout, a cancelled
-- statement, a deadlock — does NOT roll back: it leaves an INVALID index behind, which
-- is never used by the planner but IS maintained by every write. `if not exists` sees
-- that leftover and skips, so a bare re-run would "succeed" while the sweep still had no
-- usable index. The apply workflow retries a lock-timeout by simply re-running the file
-- (up to 30 attempts), so this is the likely path, not an exotic one. The guard below
-- drops the leftover first, so the re-run rebuilds cleanly.
--
-- It is scoped to `indisvalid = false` on purpose, rather than an unconditional
-- `drop index if exists`. Two reasons, both load-bearing here: (1) an unconditional drop
-- makes `if not exists` unreachable, so a re-run over a HEALTHY index would drop a good
-- index and rebuild it, leaving the sweep unindexed meanwhile — the opposite of "a
-- re-run is a no-op"; (2) `DROP INDEX` takes ACCESS EXCLUSIVE on `properties`, which is
-- STRICTER than the SHARE lock this whole migration exists to avoid, and the retry loop
-- would take it on every one of up to 30 attempts. Guarded, that lock is taken only when
-- there is actually a broken index to clean up. A plain DROP INDEX is legal inside the
-- DO block; only CREATE INDEX CONCURRENTLY is not.
--
-- Rollback: DROP INDEX CONCURRENTLY properties_repr_listing_ref_active_idx;

SET lock_timeout = '30s';
SET statement_timeout = '900s';

do $$
begin
  if exists (
        select 1
          from pg_class c
          join pg_index i on i.indexrelid = c.oid
          join pg_class t on t.oid = i.indrelid
         where c.relname = 'properties_repr_listing_ref_active_idx'
           and t.relname = 'properties'
           and not i.indisvalid) then
    raise notice 'dropping an INVALID leftover from a failed CONCURRENTLY build';
    drop index if exists public.properties_repr_listing_ref_active_idx;
  end if;
end $$;

create index concurrently if not exists properties_repr_listing_ref_active_idx
  on public.properties (repr_listing_ref_id)
  where status = 'active' and repr_listing_ref_id is not null;
