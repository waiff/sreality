-- 277_browse_read_model_refresh.sql
--
-- Unified Browse read model, slice 2: the rebuild machinery. Two SECURITY
-- DEFINER functions blue-green-rebuild the read models FROM browse_projection
-- (migration 276 — the one place the column contract + publication gate live),
-- scheduled in-DB by pg_cron. This RETIRES scripts/refresh_map_mv.py and
-- .github/workflows/refresh_map_mv.yml (deleted in the same PR): pg_cron fires
-- to the minute, while GH Actions cron is throttled/jittered (measured ~2x on
-- the scrape crons), and a pure-DB rebuild has no business running on an
-- external runner. Precedent: refresh_health_matviews(), pg_cron every 10 min
-- since migration 136.
--
-- Blue-green in ONE transaction (each function call = one tx): build the
-- replacement off to the side (no lock on the live object, the slow part),
-- ANALYZE it, then DROP live + RENAME — the ACCESS EXCLUSIVE window is only
-- the final few statements, ~100 ms. In-flight anon reads serialize with the
-- swap (a statement never straddles the DDL), and the next page request
-- resolves the unchanged NAME to the new relation. pg_notify('pgrst', ...)
-- refreshes PostgREST's schema cache after the OID change.
--
-- ANALYZE-before-swap is MANDATORY, not hygiene: autovacuum never analyzes a
-- fresh relation before its first reads, and the frontend's count=planned
-- (planner estimate) reads pg_statistic — without stats it returns garbage.
-- Pinned by tests/test_browse_read_path_guardrail.py.
--
-- Overlap guard: pg_cron does not skip a tick if the prior run is still going;
-- pg_try_advisory_lock makes the late tick a no-op instead of a pile-up. The
-- session-level lock auto-releases if the backend dies.

-------------------------------------------------------------------
-- 1. List rebuild (every 5 min): all active rows, recency-clustered.
-------------------------------------------------------------------
create or replace function rebuild_browse_list()
returns void
language plpgsql
security definer
set search_path = public
set statement_timeout = '600s'
as $fn$
declare
  t0 timestamptz := clock_timestamp();
  n  bigint;
begin
  if not pg_try_advisory_lock(hashtext('rebuild_browse_list')) then
    raise notice 'rebuild_browse_list: previous run still active, skipping tick';
    return;
  end if;
  begin
    execute 'drop table if exists browse_list_next';
    -- UNLOGGED + physical order (category, type, first_seen): every
    -- within-category query reads a contiguous band, which is what lets the
    -- index set stay lean (see migration 276's rationale).
    execute $q$
      create unlogged table browse_list_next as
      select * from browse_projection
      order by category_main, category_type, first_seen_at
    $q$;
    -- Keep in lockstep with migration 276's shell indexes.
    execute 'create unique index browse_list_next_pk on browse_list_next (property_id)';
    execute 'create index browse_list_next_cat_first_seen_idx on browse_list_next (category_main, category_type, first_seen_at desc, property_id desc)';
    execute 'create index browse_list_next_obec_price_idx on browse_list_next (obec_id, category_type, price_czk) where obec_id is not null';
    execute 'create index browse_list_next_okres_price_idx on browse_list_next (okres_id, category_type, price_czk) where okres_id is not null';
    execute 'create index browse_list_next_region_price_idx on browse_list_next (region_id, category_type, price_czk) where region_id is not null';
    execute 'analyze browse_list_next';
    execute 'select count(*) from browse_list_next' into n;

    -- The swap: short ACCESS EXCLUSIVE window.
    execute 'drop table if exists browse_list';
    execute 'alter table browse_list_next rename to browse_list';
    execute 'alter index browse_list_next_pk rename to browse_list_pk';
    execute 'alter index browse_list_next_cat_first_seen_idx rename to browse_list_cat_first_seen_idx';
    execute 'alter index browse_list_next_obec_price_idx rename to browse_list_obec_price_idx';
    execute 'alter index browse_list_next_okres_price_idx rename to browse_list_okres_price_idx';
    execute 'alter index browse_list_next_region_price_idx rename to browse_list_region_price_idx';
    execute 'grant select on browse_list to anon, authenticated';

    update browse_read_model_state
       set list_rebuilt_at  = now(),
           list_duration_ms = (extract(epoch from clock_timestamp() - t0) * 1000)::integer,
           list_rows        = n
     where id = 1;
    perform pg_notify('pgrst', 'reload schema');
  exception when others then
    perform pg_advisory_unlock(hashtext('rebuild_browse_list'));
    raise;
  end;
  perform pg_advisory_unlock(hashtext('rebuild_browse_list'));
end
$fn$;

-------------------------------------------------------------------
-- 2. Map rebuild (every 30 min): lat/lng rows, geo-clustered matview.
-------------------------------------------------------------------
-- A verbatim port of scripts/refresh_map_mv.py's refresh() into SQL, with one
-- change: it now selects FROM browse_projection (+ the lat/lng filter), so the
-- gate + column contract live once. Stays a regular MATERIALIZED VIEW (its
-- 30-min WAL cost is the long-accepted status quo; consumers keep the exact
-- relation name + kind).
create or replace function rebuild_properties_map_mv()
returns void
language plpgsql
security definer
set search_path = public
set statement_timeout = '600s'
as $fn$
declare
  t0 timestamptz := clock_timestamp();
  n  bigint;
begin
  if not pg_try_advisory_lock(hashtext('rebuild_properties_map_mv')) then
    raise notice 'rebuild_properties_map_mv: previous run still active, skipping tick';
    return;
  end if;
  begin
    execute 'drop materialized view if exists properties_map_mv_next';
    execute $q$
      create materialized view properties_map_mv_next as
      select * from browse_projection
      where lat is not null and lng is not null
      order by category_main, category_type, lat, lng
    $q$;
    execute 'create unique index properties_map_mv_next_pk on properties_map_mv_next (property_id)';
    execute $q$
      create index properties_map_mv_next_cover on properties_map_mv_next
        (category_main, category_type, lat, lng)
        include (sreality_id, price_czk, disposition, subtype, area_m2, district,
                 last_seen_at, first_seen_at, is_active)
    $q$;
    execute 'analyze properties_map_mv_next';
    execute 'select count(*) from properties_map_mv_next' into n;

    execute 'drop materialized view if exists properties_map_mv';
    execute 'alter materialized view properties_map_mv_next rename to properties_map_mv';
    execute 'alter index properties_map_mv_next_pk rename to properties_map_mv_pk';
    execute 'alter index properties_map_mv_next_cover rename to properties_map_mv_cover';
    execute 'grant select on properties_map_mv to anon, authenticated';

    update browse_read_model_state
       set map_rebuilt_at  = now(),
           map_duration_ms = (extract(epoch from clock_timestamp() - t0) * 1000)::integer,
           map_rows        = n
     where id = 1;
    perform pg_notify('pgrst', 'reload schema');
  exception when others then
    perform pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));
    raise;
  end;
  perform pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));
end
$fn$;

-------------------------------------------------------------------
-- 3. Schedules (guarded — CI's pg_cron-less replay logs a notice and skips;
--    cron.schedule upserts by job name, so re-applying is idempotent).
--    Map offset to :07/:37 so the two rebuilds never contend for I/O.
-------------------------------------------------------------------
do $cron$
begin
  create extension if not exists pg_cron;
  perform cron.schedule(
    'browse-list-rebuild',
    '*/5 * * * *',
    $$select public.rebuild_browse_list();$$
  );
  perform cron.schedule(
    'browse-map-rebuild',
    '7,37 * * * *',
    $$select public.rebuild_properties_map_mv();$$
  );
exception when others then
  raise notice 'pg_cron unavailable; browse read-model rebuilds not scheduled (%). Call rebuild_browse_list() / rebuild_properties_map_mv() from another scheduler.', sqlerrm;
end
$cron$;
