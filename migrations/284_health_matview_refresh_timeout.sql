-- 284: stop the health-dashboard matview refresh from timing out.
--
-- refresh_health_matviews() (pg_cron 'refresh-health-dashboard', every 10 min) refreshes six
-- matviews CONCURRENTLY with no statement_timeout override, so it ran on the 2-min OLTP default.
-- The heavy one (scraper_health_checks_mv) sits right at that edge — the job failed ~55% of runs
-- on `canceling statement due to statement timeout`, and a compute upgrade (2026-07-09) did NOT
-- fix it (the fleet recovered but this function still failed 4/9 in the following 90 min), because
-- the refresh is intrinsically near the ceiling, not merely starved. pg_stat_statements shows
-- comparable refreshes running 8-40s with maxes past 144s.
--
-- Fix: give the function its own generous per-statement budget. 300s covers the observed maxes
-- with headroom and stays well under the 10-min cron interval, so runs never overlap (realistic
-- total is ~2-4 min). This is a controlled internal maintenance job, not an anon query — the
-- 2-min OLTP default is the wrong ceiling for it. Body unchanged; only the SET clause is added.

CREATE OR REPLACE FUNCTION public.refresh_health_matviews()
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
 SET statement_timeout TO '300s'
AS $function$
begin
  refresh materialized view concurrently health_summary_mv;
  refresh materialized view concurrently portal_health_mv;
  refresh materialized view concurrently snapshot_churn_24h_mv;
  refresh materialized view concurrently scraper_health_checks_mv;
  refresh materialized view concurrently category_trends_mv;
  refresh materialized view concurrently health_mv_refresh_stamp;
end;
$function$;
