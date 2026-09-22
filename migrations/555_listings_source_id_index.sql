-- 555_listings_source_id_index.sql
-- The re-parse seam walks one portal's rows by id; give it an index that does the same.
--
-- scripts/reparse.py pages `WHERE l.source = %(source)s AND l.id > %(after)s ORDER BY l.id
-- LIMIT n`. With no (source, id) index the planner walks listings_pkey from `after` and
-- FILTERS on source, so the first page of a portal whose rows start late in the id space
-- reads every earlier row of every other portal before its first hit: realitymix's first
-- id is 365,203, and under the IO of the twice-hourly map-view rebuild that first page
-- overran the 120 s statement_timeout four times in a row, twice (runs 35710722269 and
-- 35717845718, 2026-09-22) and took the floor heal down at page one. The workaround --
-- `--after <min id - 1>` -- is a per-portal number a human has to know; the index makes
-- the walk O(page) for every portal from id 0.
--
-- CONCURRENTLY: listings is the hottest table; apply_migration.yml runs statement-autocommit
-- (no transaction), which is the one shape in which CONCURRENTLY is legal. Additive.
--
-- A concurrent build over 893k rows outlasts the cluster's 120 s default statement
-- timeout under the day's IO (the first apply, run 35721990100, was cancelled at that
-- mark), and a cancelled CONCURRENTLY build leaves an INVALID index behind that
-- `if not exists` would then keep. So: an explicit budget, and the invalid leftover is
-- dropped first -- both idempotent, so a re-run after any failure is safe.

-- The drop and the build take no lock that blocks anyone else, but each WAITS for the
-- transactions already using the table; while a heal pages the table every few seconds a
-- 5 s wait never lands (run 35722695489). Two minutes is the wait, not a hold.
set lock_timeout = '120s';
set statement_timeout = '1800s';

drop index concurrently if exists public.listings_source_id_idx;

create index concurrently if not exists listings_source_id_idx
    on public.listings (source, id);
