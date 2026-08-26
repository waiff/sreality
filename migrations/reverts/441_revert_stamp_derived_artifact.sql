-- 441_revert_stamp_derived_artifact.sql
--
-- REVERT for 441_stamp_derived_artifact.sql. SHIPPED UNAPPLIED.
--
-- Lives in migrations/reverts/ for the same two verified reasons as every revert in this
-- build: the CI schema replay applies `ls migrations/*.sql | sort` (NOT recursive), so a
-- revert beside its forward migration would be applied right after it and silently undo the
-- change in every replayed environment; and tests/test_migration_numbers.py globs the same
-- way and forbids a duplicate number above 304.
--
-- ORDER MATTERS AND IS THE OPPOSITE OF THE FORWARD MIGRATION'S. Four Python producers call
-- `public.stamp_derived_artifact` at runtime (scripts/resolve_brokers.py,
-- scraper/price_stats_db.py, api/rent_map.py, scripts/refresh_image_stats.py). Dropping the
-- function while that code is deployed turns every one of those call sites into a 42883
-- `function does not exist` — which, unlike the stamp itself, is NOT silent: it would abort
-- the daily broker sweep's matview step, the weekly price-stats refresh, the two-hourly
-- image-stat refresh, and the rent-map ingest endpoint. So:
--
--     1. revert the CODE first (git revert the branch's Python changes) and let it deploy;
--     2. only then run PART 2 below.
--
-- PART 1 alone (restoring refresh_health_matviews) is safe at any time and is the cheaper
-- fix if the problem is with the health fan-out specifically.
--
-- WHAT APPLYING THIS COSTS, stated plainly so it is never done casually: it returns the
-- platform to a registry that DECLARES fourteen artifacts and OBSERVES three. Eleven rows go
-- back to publishing `last_succeeded_at IS NULL` forever, and for `price_stat_choropleth` and
-- `rent_map_choropleth` there is no other freshness signal in existence — a non-concurrent
-- REFRESH swaps the heap, so pg_stat_user_tables reads zero, and pg_stat_file is denied on
-- this instance. tests/test_derived_artifacts_stamping.py goes RED as a whole, correctly.
-- Prefer fixing forward.
--
-- The body restored below is the LIVE pre-441 body, captured with pg_get_functiondef from
-- erlvtprrmrylhznfyaih on 2026-08-26 and byte-identical to migration 371's on-disk
-- definition (both 389 chars). Its md5(prosrc) is
--     refresh_health_matviews   0ba891c6126016da31cc3acebbd0170a
-- so applying PART 1 and re-hashing is a complete check that the revert is exact.

-- =======================================================================================
-- PART 1 -- the health fan-out goes back to un-instrumented. Safe on its own.
-- =======================================================================================

begin;

set local lock_timeout = '5s';

-- ci-allow-ungated: refresh_health_matviews -- unchanged posture from migrations 371/441;
-- the function only issues REFRESH MATERIALIZED VIEW and returns void.
create or replace function public.refresh_health_matviews()
returns void
language plpgsql
security definer
set search_path to 'public'
as $function$
begin
  refresh materialized view concurrently health_summary_mv;
  refresh materialized view concurrently portal_health_mv;
  refresh materialized view concurrently snapshot_churn_24h_mv;
  refresh materialized view concurrently scraper_health_checks_mv;
  refresh materialized view concurrently category_trends_mv;
  refresh materialized view concurrently health_mv_refresh_stamp;
end;
$function$;

commit;

-- =======================================================================================
-- PART 2 -- the helper goes. ONLY after the Python callers are no longer deployed.
-- =======================================================================================
--
-- Verify first that nothing in the catalog still calls it:
--   select proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace
--    where n.nspname = 'public' and p.prosrc like '%stamp_derived_artifact%';   -- => 0 rows
-- and that the deployed image no longer contains the four Python call sites.
--
-- The eleven rows this drop strands keep whatever last_succeeded_at they last received;
-- nothing advances them again. That is the point of the cost note above.

begin;

drop function if exists public.stamp_derived_artifact(text, bigint, integer);

commit;
