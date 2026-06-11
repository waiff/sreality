-- 176_health_checks_v2.sql
--
-- Close four verified observability blind spots in one health-checks revision,
-- plus make Health-matview staleness itself observable:
--
--   1. delisting_latency — how long a delisted listing stayed nominally active
--      (inactive_at - last_seen_at, over rows flipped in the last 7 days).
--      Needs migration 175's listings.inactive_at; historical rows are NULL
--      and ignored, so the metric accrues from the day 175 landed.
--   2. snapshot_churn — snapshots written in 24h vs active listing count. The
--      idnes A/B/A hash-thrash storm re-snapshotted tens of thousands of
--      listings for WEEKS with zero signal; a ratio near 1 means every active
--      listing is re-snapshotting daily.
--   3. field_null_drift — per-field population vs the daily data_quality
--      baseline. The bazos locality breakage (100% NULL for weeks) had the
--      view (migration 123) but nothing capturing or comparing snapshots; a
--      pg_cron job below captures daily, the check compares live vs baseline.
--   4. e2e_latency — the composed portal→Browse number (detail-drain p90 +
--      property-attach oldest), so end-to-end staleness is one check, not two
--      segments the operator must add up mentally.
--
-- scraper_health_checks is SECURITY INVOKER (anon), and inactive_at /
-- listing_snapshots / data_quality_* aren't anon-exposed, so each new check
-- reads through a tiny SECURITY DEFINER stat helper that returns only
-- aggregates — the migration 170 unattached_listings_stat pattern. The
-- function body below is the CURRENT live body (migration 170, verified
-- byte-identical to production) carrying ALL twelve existing checks forward,
-- with the new CTEs threaded in.
--
-- Also here: a refresh stamp for the pg_cron-refreshed Health matviews. The
-- health_summary_mv payload had no generated_at, so a dead pg_cron silently
-- served week-old numbers as fresh. A one-row stamp matview is refreshed by
-- the same refresh_health_matviews() call; health_summary() overlays it as
-- 'generated_at' so the frontend can warn when the refresh loop stops.

------------------------------------------------------------------
-- 1. Index for the snapshot_churn 24h scan (live RPC path). Only a
--    per-listing (sreality_id, scraped_at) index existed; a bare scraped_at
--    btree is broadly useful for any recent-snapshots query.
------------------------------------------------------------------

create index if not exists listing_snapshots_scraped_at_idx
  on listing_snapshots (scraped_at);

------------------------------------------------------------------
-- 2. SECURITY DEFINER stat helpers (aggregates only — no row data leaks).
------------------------------------------------------------------

create or replace function public.delisting_latency_stat(p_source text)
 returns table(n int, p50_min numeric, p90_min numeric)
 language sql
 stable
 security definer
 set search_path = public
as $function$
  select count(*)::int,
         coalesce(round((percentile_cont(0.5) within group
           (order by extract(epoch from inactive_at - last_seen_at)/60.0))::numeric, 1), 0),
         coalesce(round((percentile_cont(0.9) within group
           (order by extract(epoch from inactive_at - last_seen_at)/60.0))::numeric, 1), 0)
  from listings
  where source = p_source
    and inactive_at is not null
    and inactive_at > now() - interval '7 days'
$function$;

grant execute on function public.delisting_latency_stat(text) to anon, authenticated;

create or replace function public.snapshot_churn_stat(p_source text)
 returns table(snaps_24h bigint, active_n bigint)
 language sql
 stable
 security definer
 set search_path = public
as $function$
  select
    (select count(*) from listing_snapshots s
       join listings l on l.sreality_id = s.sreality_id
       where s.scraped_at > now() - interval '24 hours' and l.source = p_source),
    (select count(*) from listings where source = p_source and is_active)
$function$;

grant execute on function public.snapshot_churn_stat(text) to anon, authenticated;

-- Live side is a targeted one-scan SELECT over the key fields, NOT the full
-- data_quality_by_source view (29 fields over every source — too heavy for a
-- per-source request-path call). Baseline = the latest daily capture per field
-- that is 20h-8d old: old enough that today's capture can't mask a fresh
-- breakage, young enough that a long-dead capture job can't linger as truth.
create or replace function public.field_null_drift_stat(p_source text)
 returns table(field text, baseline_pct numeric, live_pct numeric, drift_pts numeric)
 language sql
 stable
 security definer
 set search_path = public
as $function$
  with live as (
    select count(*)::numeric as n,
           count(price_czk)::numeric as price_czk,
           count(area_m2)::numeric as area_m2,
           count(geom)::numeric as geom,
           count(locality)::numeric as locality,
           count(disposition)::numeric as disposition
    from listings
    where source = p_source and is_active
  ),
  live_long as (
    select v.field,
           round(100.0 * v.populated / nullif(live.n, 0), 1) as live_pct
    from live,
    lateral (values
      ('price_czk',   live.price_czk),
      ('area_m2',     live.area_m2),
      ('geom',        live.geom),
      ('locality',    live.locality),
      ('disposition', live.disposition)
    ) as v(field, populated)
  ),
  baseline as (
    select distinct on (dqs.field) dqs.field, dqs.pct_populated
    from data_quality_snapshots dqs
    where dqs.source = p_source
      and dqs.field in ('price_czk', 'area_m2', 'geom', 'locality', 'disposition')
      and dqs.captured_at < now() - interval '20 hours'
      and dqs.captured_at > now() - interval '8 days'
    order by dqs.field, dqs.captured_at desc
  )
  select ll.field, b.pct_populated, ll.live_pct, b.pct_populated - ll.live_pct
  from live_long ll
  join baseline b on b.field = ll.field
$function$;

grant execute on function public.field_null_drift_stat(text) to anon, authenticated;

------------------------------------------------------------------
-- 3. scraper_health_checks: migration 170 body + delist/churn/drift CTEs and
--    the four new checks (delisting_latency after delisting_spike,
--    snapshot_churn after error_rate, field_null_drift after stale_active,
--    e2e_latency after property_attach_lag).
------------------------------------------------------------------

create or replace function public.scraper_health_checks(p_source text default 'sreality'::text)
 returns jsonb
 language sql
 stable
as $function$
with
runs24 as (
  select * from scrape_runs_public
  where started_at > now() - interval '24 hours' and source = p_source
),
cad as (
  select coalesce((select scrape_cadence_minutes from portals_public where source = p_source), 60) as mins
),
m as (
  select
    extract(epoch from now() - (select max(started_at) from scrape_runs_public where index_pages > 0 and source = p_source))/60.0 as mins_since_start,
    (select max(started_at) from scrape_runs_public where index_pages > 0 and source = p_source) as last_start,
    (select count(*) from scrape_runs_public
       where ended_at is null and started_at < now() - interval '30 minutes'
         and started_at > now() - interval '6 hours' and source = p_source) as stuck,
    coalesce((select sum(listings_scraped_new) from runs24), 0) as scraped_new,
    coalesce((select sum(listings_updated) from runs24), 0) as updated,
    coalesce((select max(listings_inactive) from runs24), 0) as inactive_max,
    coalesce((select sum(errors) from runs24), 0) as errors_sum,
    (select count(*) from listings_public where source = p_source and first_seen_at > now() - interval '24 hours') as new_listings_fs,
    (select count(*) filter (where is_active and last_seen_at < now() - interval '7 days')
       from listings_public where source = p_source) as stale_active,
    extract(epoch from now() - (select max(last_seen_at) from listings_public where is_active and source = p_source))/60.0 as mins_fresh,
    (select count(*) filter (where not f.given_up)
       from listing_fetch_failures_public f
       left join listings_public l on l.sreality_id = f.sreality_id
       where coalesce(l.source, 'sreality') = p_source) as active_fail,
    (select count(*) filter (where f.given_up)
       from listing_fetch_failures_public f
       left join listings_public l on l.sreality_id = f.sreality_id
       where coalesce(l.source, 'sreality') = p_source) as given_up,
    (select round(100.0 * count(*) filter (where i.storage_path is not null) / nullif(count(*), 0), 1)
       from images_public i join listings_public l on l.sreality_id = i.sreality_id
       where l.is_active and l.source = p_source) as img_pct
),
calc as (
  select *, round(100.0 * errors_sum / nullif(errors_sum + scraped_new + updated, 0), 1) as err_pct from m
),
queue as (
  select
    count(*) filter (where claimed_at is null and not given_up) as claimable,
    count(*) filter (where claimed_at is null and not given_up and priority = 1) as changed,
    count(*) filter (where given_up) as given_up
  from listing_detail_queue_public where source = p_source
),
lag as (
  select
    coalesce(round((percentile_cont(0.5) within group (order by extract(epoch from now() - enqueued_at)/60.0))::numeric, 1), 0) as p50_min,
    coalesce(round((percentile_cont(0.9) within group (order by extract(epoch from now() - enqueued_at)/60.0))::numeric, 1), 0) as p90_min,
    count(*) filter (where enqueued_at < now() - make_interval(mins => (select (mins * 3)::int from cad)))::int as unhealthy_n,
    count(*)::int as n
  from listing_detail_queue_public
  where claimed_at is null and not given_up and priority <> 2 and source = p_source
),
attach as (
  select n, oldest_min from unattached_listings_stat(p_source)
),
delist as (
  select n, p50_min, p90_min from delisting_latency_stat(p_source)
),
churn as (
  select snaps_24h, active_n,
         round(snaps_24h / nullif(active_n, 0)::numeric, 2) as ratio
  from snapshot_churn_stat(p_source)
),
drift as (
  select count(*)::int as n_fields,
         coalesce(max(drift_pts), 0) as max_drift,
         (array_agg(field order by drift_pts desc))[1] as worst_field
  from field_null_drift_stat(p_source)
),
recon as (
  select count(*) as n_with_data, max(gap_pct) as max_gap_pct
  from (
    select abs((e->>'collected')::numeric - (e->>'sreality_result_size')::numeric)
             / nullif((e->>'sreality_result_size')::numeric, 0) * 100.0 as gap_pct
    from (
      select by_category from scrape_runs_public
      where ended_at is not null and index_pages > 0 and source = p_source
      order by started_at desc limit 1
    ) latest,
    lateral jsonb_array_elements(coalesce(latest.by_category, '[]'::jsonb)) e
    where (e->>'sreality_result_size') is not null and (e->>'collected') is not null
      and (e->>'sreality_result_size')::numeric > 0
  ) d
)
select jsonb_build_object(
  'generated_at', now(), 'source', p_source,
  'checks', jsonb_build_array(
    jsonb_build_object(
      'key', 'liveness', 'label', 'Scraper running on schedule',
      'status', case when last_start is null then 'warn'
                     when mins_since_start < cad.mins * 1.5 then 'pass'
                     when mins_since_start < cad.mins * 3 then 'warn' else 'fail' end,
      'value', case when last_start is null then 'never'
                    else coalesce(round(mins_since_start::numeric, 0)::text, '–') || ' min ago' end,
      'detail', 'Last index walk started ' || coalesce(to_char(last_start, 'YYYY-MM-DD HH24:MI'), 'never')
                || ' UTC. Expected cadence ~' || cad.mins::text || ' min (GitHub throttles short crons). '
                || 'Warn >' || round(cad.mins * 1.5)::text || ' min, fail >' || round(cad.mins * 3)::text || ' min.'),
    jsonb_build_object('key', 'runs_completing', 'label', 'Runs finishing cleanly',
      'status', case when stuck = 0 then 'pass' when stuck = 1 then 'warn' else 'fail' end,
      'value', stuck::text || ' stuck',
      'detail', 'Index-walk or detail-drain runs started >30 min ago (last 6h) that never recorded an end timestamp — a crash or timeout before finalize. Expected 0.'),
    jsonb_build_object('key', 'new_listings', 'label', 'New listings flowing',
      'status', case when new_listings_fs > 0 then 'pass' else 'warn' end,
      'value', new_listings_fs::text || ' / 24h',
      'detail', 'New listings first seen in the last 24h (from listings.first_seen_at — immune to a crashed or SIGKILLed drain''s lost run counters). 0 over a full day suggests the index-walk enqueue or the detail-drain is blocked.'),
    jsonb_build_object('key', 'delisting_spike', 'label', 'No false mass-delisting',
      'status', case when inactive_max <= 500 then 'pass' when inactive_max <= 2000 then 'warn' else 'fail' end,
      'value', inactive_max::text || ' max/run',
      'detail', 'Largest single-run inactivation in 24h (the index-walk''s mark_inactive). A big spike usually means a truncated index walk falsely delisted live listings; the walk-completeness guard mitigates this. Warn >500, fail >2000.'),
    jsonb_build_object('key', 'delisting_latency', 'label', 'Delisting latency (gone → flipped)',
      'status', case when delist.n = 0 then 'pass'
                     when delist.p90_min < 2160 then 'pass'
                     when delist.p90_min < 4320 then 'warn' else 'fail' end,
      'value', case when delist.n = 0 then 'no flips recorded yet'
                    else 'p50 ' || delist.p50_min::text || 'm / p90 ' || delist.p90_min::text || 'm' end,
      'detail', 'How long a delisted listing stayed nominally active: inactive_at − last_seen_at over the '
                || delist.n::text || ' listings flipped inactive in the last 7 days. Rows flipped before migration 175 carry no stamp and are ignored. Warn p90 >36h (2160 min), fail >72h (4320 min).'),
    jsonb_build_object('key', 'error_rate', 'label', 'Detail-fetch error rate',
      'status', case when coalesce(err_pct, 0) < 5 then 'pass' when coalesce(err_pct, 0) < 15 then 'warn' else 'fail' end,
      'value', coalesce(err_pct, 0)::text || '%',
      'detail', 'Errors as a share of detail work (errors + new + updated) over 24h. Elevated values usually mean the portal is rate-limiting. Warn >5%, fail >15%.'),
    jsonb_build_object('key', 'snapshot_churn', 'label', 'Snapshot churn (hash thrash)',
      'status', case when coalesce(churn.ratio, 0) < 0.5 then 'pass'
                     when coalesce(churn.ratio, 0) < 1.5 then 'warn' else 'fail' end,
      'value', coalesce(churn.ratio, 0)::text || '× / 24h',
      'detail', churn.snaps_24h::text || ' snapshots written in the last 24h across ' || churn.active_n::text
                || ' active listings. A ratio near 1 means the average listing re-snapshots DAILY — almost always a volatile field thrashing the content hash (the idnes A/B/A storm ran for weeks undetected), not real market churn. Warn ≥0.5, fail ≥1.5.'),
    jsonb_build_object('key', 'stale_active', 'label', 'No stale active listings',
      'status', case when stale_active < 50 then 'pass' when stale_active < 500 then 'warn' else 'fail' end,
      'value', stale_active::text,
      'detail', 'Listings still is_active=true but not seen in the index for >7 days — they should have been marked inactive. Warn >50, fail >500.'),
    jsonb_build_object('key', 'field_null_drift', 'label', 'Field completeness drift',
      'status', case when drift.n_fields = 0 then 'pass'
                     when drift.max_drift < 5 then 'pass'
                     when drift.max_drift < 15 then 'warn' else 'fail' end,
      'value', case when drift.n_fields = 0 then 'no baseline yet'
                    else drift.worst_field || ' −' || round(greatest(drift.max_drift, 0), 1)::text || ' pts' end,
      'detail', 'Largest drop in field population (percentage points) vs the daily data-quality baseline (data_quality_snapshots, latest capture 20h–8d old), across price_czk / area_m2 / geom / locality / disposition. Catches a parser silently losing a field within a day — the bazos locality breakage took weeks to surface this way. Warn ≥5 pts, fail ≥15 pts.'),
    jsonb_build_object('key', 'fetch_failures', 'label', 'Fetch-failure backlog',
      'status', case when active_fail < 1000 then 'pass' when active_fail < 5000 then 'warn' else 'fail' end,
      'value', active_fail::text || ' active',
      'detail', calc.given_up::text || ' listings given up after repeated failures. Active failures retry with priority next run. Warn >1000, fail >5000.'),
    jsonb_build_object('key', 'detail_queue_backlog', 'label', 'Detail-drain backlog',
      'status', case when queue.claimable < 2000 then 'pass' when queue.claimable < 10000 then 'warn' else 'fail' end,
      'value', queue.claimable::text || ' queued',
      'detail', 'New + price-changed listings the index walk enqueued but the detail-drain has not fetched yet ('
                || queue.changed::text || ' price-changed). A new listing becomes an active row only once drained, so THIS backlog — not data loss — is what opens the gap in "Index walk completeness". The drain closes it; raise its cap/cadence if it grows. '
                || queue.given_up::text || ' given up. Warn >2k, fail >10k.'),
    jsonb_build_object('key', 'detail_queue_lag', 'label', 'Detail-drain lag (index→fetch)',
      'status', case when lag.n = 0 then 'pass'
                     when lag.p90_min < cad.mins * 1.5 then 'pass'
                     when lag.p90_min < cad.mins * 3 then 'warn' else 'fail' end,
      'value', case when lag.n = 0 then 'empty'
                    else 'p50 ' || lag.p50_min::text || 'm / p90 ' || lag.p90_min::text || 'm' end,
      'detail', 'Time between the index walk enqueueing a listing and the detail-drain fetching it, over listings still waiting (in-flight only — completed queue rows are deleted, so a caught-up drain reads empty). '
                || lag.unhealthy_n::text || ' have waited >' || round(cad.mins * 3)::text || ' min (~3 missed cycles). '
                || 'Fresh + price-changed rows only (excludes failure-retry). Warn p90 >' || round(cad.mins * 1.5)::text || ' min, fail p90 >' || round(cad.mins * 3)::text || ' min.'),
    jsonb_build_object('key', 'property_attach_lag', 'label', 'Property attach lag (Browse-visible)',
      'status', case when attach.n = 0 then 'pass'
                     when attach.oldest_min < 30 then 'pass'
                     when attach.oldest_min < 90 then 'warn' else 'fail' end,
      'value', case when attach.n = 0 then 'all attached'
                    else attach.n::text || ' waiting, oldest ' || attach.oldest_min::text || 'm' end,
      'detail', 'A scraped listing lands with no properties row and is invisible in Browse (which reads the property grain) until the async property-maintenance job (recompute_property_stats --incremental, ~every 5 min; daily full sweep as backstop) attaches it as a singleton. The remaining gap between "scraped into listings" and "Browse-visible" — pairs with the detail-drain lag above for end-to-end latency. Warn oldest >30 min, fail >90 min.'),
    jsonb_build_object('key', 'e2e_latency', 'label', 'End-to-end latency (portal → Browse)',
      'status', case when (lag.p90_min + attach.oldest_min) < 90 then 'pass'
                     when (lag.p90_min + attach.oldest_min) < 240 then 'warn' else 'fail' end,
      'value', round((lag.p90_min + attach.oldest_min)::numeric, 0)::text || ' min',
      'detail', 'Composed pipeline latency: detail-drain p90 (' || lag.p90_min::text
                || 'm, index-seen → fetched) + oldest unattached listing (' || attach.oldest_min::text
                || 'm, fetched → Browse-visible). The two segment checks above are the components; this is the single "how far behind the portal is Browse" number. Warn ≥90 min, fail ≥240 min.'),
    jsonb_build_object('key', 'data_freshness', 'label', 'Data freshness',
      'status', case when mins_fresh is null then 'warn'
                     when mins_fresh < cad.mins then 'pass'
                     when mins_fresh < cad.mins * 3 then 'warn' else 'fail' end,
      'value', case when mins_fresh is null then '–'
                    else coalesce(round(mins_fresh::numeric, 0)::text, '–') || ' min' end,
      'detail', 'Time since the most recently seen active listing. Warn >' || cad.mins::text || ' min, fail >' || round(cad.mins * 3)::text || ' min.'),
    jsonb_build_object('key', 'index_completeness', 'label', 'Index walk completeness',
      'status', case when recon.n_with_data = 0 then 'warn'
                  when coalesce(recon.max_gap_pct, 0) < 2 then 'pass'
                  when coalesce(recon.max_gap_pct, 0) < 5 then 'warn' else 'fail' end,
      'value', case when recon.n_with_data = 0 then 'no data yet'
                    else round(coalesce(recon.max_gap_pct, 0), 1)::text || '% max gap' end,
      'detail', 'Largest per-category gap between how many index entries we collected and the portal''s reported result_size on the latest completed index walk — i.e. did the walk SEE every listing. Whether we have FETCHED them is the separate detail-drain backlog. Populates once the walk records per-category result_size. Warn >2%, fail >5%.')
  ))
from calc, recon, queue, cad, lag, attach, delist, churn, drift;
$function$;

grant execute on function public.scraper_health_checks(text) to anon, authenticated;

------------------------------------------------------------------
-- 4. Daily data-quality capture — the missing half of migration 123. The view
--    and the snapshot table have existed since 123, but nothing captured
--    periodically, so there was never a baseline to drift against. Guarded
--    do-block per migration 136 so CI's pg_cron-less replay still applies.
------------------------------------------------------------------

do $cron$
begin
  create extension if not exists pg_cron;
  perform cron.schedule(
    'capture-data-quality',
    '30 2 * * *',
    $$insert into public.data_quality_snapshots (source, field, n_active, n_populated, pct_populated)
      select source, field, n_active, n_populated, pct_populated from public.data_quality_by_source;$$
  );
exception when others then
  raise notice 'pg_cron unavailable; daily data-quality capture not scheduled (%). Insert from data_quality_by_source on another scheduler.', sqlerrm;
end
$cron$;

------------------------------------------------------------------
-- 5. Health matview refresh stamp. now() in a matview body is evaluated at
--    REFRESH time, so the stamp is exactly "when did the pg_cron loop last
--    run". health_summary() overlays it as generated_at; the frontend warns
--    when it ages past ~25 min (the loop runs every 10).
------------------------------------------------------------------

create materialized view if not exists health_mv_refresh_stamp as
  select 1 as id, now() as refreshed_at;

create unique index if not exists health_mv_refresh_stamp_pk
  on health_mv_refresh_stamp (id);

grant select on health_mv_refresh_stamp to anon, authenticated;

create or replace function refresh_health_matviews()
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  refresh materialized view concurrently health_summary_mv;
  refresh materialized view concurrently portal_health_mv;
  refresh materialized view concurrently health_mv_refresh_stamp;
end;
$$;

create or replace function health_summary()
returns jsonb
language sql
stable
security invoker
as $$
  select payload || jsonb_build_object(
    'generated_at', (select refreshed_at from health_mv_refresh_stamp)
  )
  from health_summary_mv;
$$;

grant execute on function health_summary() to anon;
