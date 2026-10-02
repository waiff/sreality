-- 578_matview_refresh_chokepoint.sql
-- Broker Unify W1: ONE way to publish a served matview, and the covering index
-- migration 508 silently lost.
--
-- WHY (the 2026-09-30 Brokers outage, root-caused that day):
-- scripts/resolve_brokers.py refreshed broker_region_type_stats with the plain
-- (ACCESS EXCLUSIVE) REFRESH at the tail of the daily full sweep. Every reader —
-- the leaderboard fast arms, broker detail region_shares, broker_geo_options,
-- api/outreach — queued behind the lock for the whole rebuild (352 s that day),
-- was cancelled by the 120 s statement_timeout, and the SPA showed a ~4-minute
-- silent spinner. The comment defending the plain form claimed CONCURRENTLY
-- cannot run inside a transaction; that restriction is CREATE INDEX's, not
-- REFRESH's — this repo already runs concurrent refreshes inside transactions
-- (refresh_health_matviews, refresh_location_pin_audit_mv, api/rent_map.py).
-- The same false claim shaped scripts/refresh_image_stats.py. PR #1656 fixed the
-- one site; this migration makes the whole class unrepresentable: every Python
-- refresh goes through public.refresh_matview below, and
-- tests/test_matview_refresh_convention.py fails CI on any new bypass.
--
-- The function:
--   * REFUSES a name with no derived_artifacts row (registration is the
--     contract; the silent-no-op stamp hole closes for matview producers),
--   * refreshes CONCURRENTLY, falling back to the plain form only when the
--     matview is unpopulated (WITH NO DATA first populate — the one case
--     Postgres forbids CONCURRENTLY),
--   * skips (returns -1) when another refresh of the same matview is in flight
--     — pg_try_advisory_xact_lock is transaction-scoped, so it is pooler-safe
--     here: the whole function call is one statement on one backend, and the
--     lock cannot strand (the session-scoped hazard in the database skill does
--     not apply to xact-scoped locks),
--   * stamps the registry with real rows + duration_ms in the refresh's own
--     transaction, so a failed refresh leaves no stamp.
-- SECURITY INVOKER: every legitimate caller (Python service role, pg_cron) runs
-- as the matview owner already; the function must not widen anyone's rights.
--
-- INDEX CORRECTION: migration 508's header claimed the matview's "two indexes
-- ... are unchanged", but the object had three — 508's DROP+CREATE silently lost
-- migration 414's broker_region_type_stats_geo_covering_idx, and the fast arm's
-- index-only scan (56 ms / ~1k buffers warm) regressed to a heap-visiting scan
-- (3.2–5.4 s / 20.5k buffers). Per rule #1 the record is corrected here, in a
-- new migration, never by editing 508. rank_idx is rebuilt as a covering index
-- (same key, INCLUDE carries every remaining column), so the matview stays at
-- two indexes and all four fast arms can plan index-only scans again.
-- tests/test_matview_indexes.py pins the index set from now on.

set local lock_timeout = '5s';

create or replace function public.refresh_matview(p_name text)
returns bigint
language plpgsql
security invoker
set search_path = public, pg_catalog
as $fn$
declare
  v_start timestamptz := clock_timestamp();
  v_populated boolean;
  v_rows bigint;
begin
  -- Matview existence first: a producer racing its own migration (the script
  -- runs before the migration that creates the matview is applied) gets
  -- undefined_table, which callers with a deploy-race tolerance already catch.
  select m.ispopulated into v_populated
    from pg_matviews m
   where m.schemaname = 'public' and m.matviewname = p_name;
  if v_populated is null then
    raise exception 'refresh_matview: public.% is not a materialized view', p_name
      using errcode = 'undefined_table';
  end if;

  -- Registration second, and loud: a matview that exists but has no registry
  -- row is a misconfiguration, not a race. This replaces the silent-no-op
  -- stamp for every matview producer.
  if not exists (select 1 from derived_artifacts da where da.name = p_name) then
    raise exception
      'refresh_matview: % has no derived_artifacts row — register the artifact first (database skill)',
      p_name;
  end if;

  if not pg_try_advisory_xact_lock(hashtext('refresh_matview:' || p_name)) then
    return -1;  -- another refresh of this matview is in flight; skip, never queue
  end if;

  if v_populated then
    execute format('refresh materialized view concurrently %I', p_name);
  else
    execute format('refresh materialized view %I', p_name);
  end if;

  execute format('select count(*) from %I', p_name) into v_rows;
  perform public.stamp_derived_artifact(
    p_name, v_rows,
    (extract(epoch from (clock_timestamp() - v_start)) * 1000)::int);
  return v_rows;
end
$fn$;

comment on function public.refresh_matview(text) is
  'The ONE publication path for served matviews (Broker Unify W1). Refuses '
  'unregistered names, refreshes CONCURRENTLY (plain only on first populate), '
  'skips when a refresh of the same matview is in flight (-1), stamps '
  'derived_artifacts with rows + duration_ms. Python callers use '
  'scraper.db.refresh_matview.';

-- This project's default ACL grants EXECUTE on new functions to anon /
-- authenticated (migration 287's lesson), and PostgreSQL itself grants PUBLIC.
revoke execute on function public.refresh_matview(text) from public;
revoke execute on function public.refresh_matview(text) from anon;
revoke execute on function public.refresh_matview(text) from authenticated;

-- Restore 414's covering behaviour: same rank key, INCLUDE the payload, so the
-- leaderboard fast arms are index-only again. Two indexes total, as before.
drop index if exists public.broker_region_type_stats_rank_idx;
create index broker_region_type_stats_rank_idx
  on public.broker_region_type_stats
  (geo_level, geo_id, category_main, category_type, active_property_count desc)
  include (broker_id, listing_count, property_count, active_listing_count);
