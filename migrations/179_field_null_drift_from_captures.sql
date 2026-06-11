-- 179_field_null_drift_from_captures.sql
--
-- migration 176's field_null_drift_stat computed the LIVE side by scanning all
-- active listings of the source inside the anon-called scraper_health_checks
-- RPC — verified post-apply to blow anon's 3s statement_timeout. Drift now
-- compares the two most recent data_quality_snapshots captures (fresh = <20h,
-- baseline = 20h–8d), so the request path reads only the tiny snapshots table.
-- Detection latency is bounded by the capture cadence, which moves from daily
-- to every 6h (cron.schedule upserts by jobname). A dead capture job now
-- surfaces as 'no baseline yet' instead of silently serving stale truth.

create or replace function public.field_null_drift_stat(p_source text)
 returns table(field text, baseline_pct numeric, live_pct numeric, drift_pts numeric)
 language sql
 stable
 security definer
 set search_path = public
as $function$
  with fresh as (
    select distinct on (field) field, pct_populated
    from data_quality_snapshots
    where source = p_source
      and field in ('price_czk', 'area_m2', 'geom', 'locality', 'disposition')
      and captured_at > now() - interval '20 hours'
    order by field, captured_at desc
  ),
  baseline as (
    select distinct on (field) field, pct_populated
    from data_quality_snapshots
    where source = p_source
      and field in ('price_czk', 'area_m2', 'geom', 'locality', 'disposition')
      and captured_at < now() - interval '20 hours'
      and captured_at > now() - interval '8 days'
    order by field, captured_at desc
  )
  select f.field, b.pct_populated, f.pct_populated, b.pct_populated - f.pct_populated
  from fresh f
  join baseline b using (field)
$function$;

do $cron$
begin
  create extension if not exists pg_cron;
  perform cron.schedule(
    'capture-data-quality',
    '30 */6 * * *',
    $$insert into public.data_quality_snapshots (source, field, n_active, n_populated, pct_populated)
      select source, field, n_active, n_populated, pct_populated from public.data_quality_by_source;$$
  );
exception when others then
  raise notice 'pg_cron unavailable; data-quality capture not rescheduled (%).', sqlerrm;
end
$cron$;
