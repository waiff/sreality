-- 180_snapshot_churn_matview.sql
--
-- Companion to 179: snapshot_churn_stat's live 24h listing_snapshots join
-- also blows anon's 3s statement_timeout inside scraper_health_checks.
-- Precompute the per-source ratio inputs into a matview refreshed by the same
-- 10-min pg_cron loop as the other Health matviews; the stat helper keeps its
-- signature and just reads the matview, so scraper_health_checks is untouched.
-- now() in the matview body evaluates at REFRESH time — the 24h window rolls
-- with the 10-min refresh.

create materialized view if not exists snapshot_churn_24h_mv as
  with snaps as (
    select l.source, count(*) as snaps_24h
    from listing_snapshots s
    join listings l on l.sreality_id = s.sreality_id
    where s.scraped_at > now() - interval '24 hours'
    group by 1
  ),
  act as (
    select source, count(*) as active_n
    from listings
    where is_active
    group by 1
  )
  select a.source, coalesce(sn.snaps_24h, 0)::bigint as snaps_24h, a.active_n::bigint
  from act a
  left join snaps sn using (source);

create unique index if not exists snapshot_churn_24h_mv_pk
  on snapshot_churn_24h_mv (source);

create or replace function public.snapshot_churn_stat(p_source text)
 returns table(snaps_24h bigint, active_n bigint)
 language sql
 stable
 security definer
 set search_path = public
as $function$
  select coalesce(m.snaps_24h, 0), coalesce(m.active_n, 0)
  from (select 1) one
  left join snapshot_churn_24h_mv m on m.source = p_source
$function$;

create or replace function refresh_health_matviews()
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  refresh materialized view concurrently health_summary_mv;
  refresh materialized view concurrently portal_health_mv;
  refresh materialized view concurrently snapshot_churn_24h_mv;
  refresh materialized view concurrently health_mv_refresh_stamp;
end;
$$;
