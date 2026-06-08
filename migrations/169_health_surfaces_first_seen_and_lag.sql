-- 169_health_surfaces_first_seen_and_lag.sql
--
-- Make the Health "new listings" rollups honest, and add an index->detail lag check.
--
-- Why: a crashed or SIGKILLed detail-drain used to leave scrape_runs counters at 0
-- despite committed writes (now fixed in code by per-chunk bumps, but historical
-- rows + any future gap still under-report). The per-portal "new 7d" tile
-- (portal_health_mv.scraped_new_7d) and the scraper_health_checks "new_listings"
-- 24h check both summed scrape_runs.listings_scraped_new, so they under-reported
-- ~2x (sreality 3465 vs first_seen 6842; remax ~6x) and the 24h check could falsely
-- WARN "0 / 24h". Repoint BOTH rollups to listings.first_seen_at — the crash-immune
-- source health_summary_mv.new_per_day_14d already uses — so the two Health "new"
-- surfaces agree. Keep scrape_runs.listings_scraped_new (now crash-survivable, B)
-- for the per-run drill-down AND as the err_pct denominator (errors as a share of
-- detail WORK done = errors + new + updated; first_seen excludes "updated" work).
--
-- Also add detail_queue_lag: percentiles of how long still-unfetched enqueued
-- listings have waited (the index-seen -> detail-fetched gap). In-flight only:
-- completed queue rows are deleted, so this measures the current backlog's age, not
-- historical completion latency — a chronically-behind drain shows here while it is
-- still behind. Excludes failure-retry rows (priority 2: already fetched, re-queued)
-- so it reflects genuine first-fetch latency, not retry churn.
--
-- Both objects are precompute/rollup reads off the anon 3s-timeout request path
-- (the matview is pg_cron-refreshed; the function reads it + the _public views).
-- The matview JSON key 'scraped_new_7d' and the checks[] {key,label,status,value,
-- detail} shape are unchanged, so the frontend is untouched. ADDITIVE: no base
-- table/column dropped, no type change, no source-of-truth data touched (the matview
-- is a derived rollup, rebuilt + repopulated in this same migration).

-- 1) Index backing the per-source first_seen counts. source-first because the
--    health-check query filters source by equality; it also serves the matview's
--    per-source recent-tail scan. (A partial `where first_seen_at > now()-...` index
--    is invalid — now() is not IMMUTABLE.)
create index if not exists listings_first_seen_source_idx
  on listings (source, first_seen_at);

-- 2) Recreate portal_health_mv with scraped_new_7d sourced from first_seen_at.
--    CREATE OR REPLACE MATERIALIZED VIEW does not exist; nothing depends on the
--    matview (pg_depend confirms no dependent views/rules — portal_health_summary
--    is a string-body SQL function that resolves the name at call time), so a plain
--    DROP is safe and does not need CASCADE.
drop materialized view if exists portal_health_mv;

create materialized view portal_health_mv as
  select 1 as id,
    coalesce(jsonb_agg(jsonb_build_object(
      'source', p.source,
      'label', p.label,
      'kind', p.kind,
      'stage', p.stage,
      'home_url', p.home_url,
      'listings_total', coalesce(lc.listings_total, 0::bigint),
      'listings_active', coalesce(lc.listings_active, 0::bigint),
      'listings_active_7d', coalesce(lc.listings_active_7d, 0::bigint),
      'parses_total', coalesce(pa.parses_total, 0::bigint),
      'parses_30d', coalesce(pa.parses_30d, 0::bigint),
      'last_scrape_at', sr.last_run_at,
      'runs_7d', coalesce(sr.runs_7d, 0::bigint),
      'scraped_new_7d', coalesce(fsn.first_seen_7d, 0::bigint),
      'inactive_7d', coalesce(sr.inactive_7d, 0::bigint),
      'errors_7d', coalesce(sr.errors_7d, 0::bigint),
      'last_parsed_at', pa.last_parsed_at,
      'last_activity_at', greatest(sr.last_run_at, pa.last_parsed_at, lc.last_seen_at)
    ) order by p.sort_order, p.label), '[]'::jsonb) as payload
  from portals_public p
    left join portal_listing_counts lc on lc.source = p.source
    left join parsed_url_activity pa on pa.source = p.source
    left join (
      select source,
        max(ended_at) as last_run_at,
        count(*) filter (where started_at > now() - interval '7 days') as runs_7d,
        coalesce(sum(listings_inactive) filter (where started_at > now() - interval '7 days'), 0::bigint) as inactive_7d,
        coalesce(sum(errors) filter (where started_at > now() - interval '7 days'), 0::bigint) as errors_7d
      from scrape_runs_public
      group by source
    ) sr on sr.source = p.source
    left join (
      select source, count(*)::bigint as first_seen_7d
      from listings_public
      where first_seen_at > now() - interval '7 days'
      group by source
    ) fsn on fsn.source = p.source
  where p.is_enabled;

-- Unique index required for REFRESH MATERIALIZED VIEW CONCURRENTLY (pg_cron job).
create unique index portal_health_mv_pk on public.portal_health_mv using btree (id);
-- Restore the anon/authenticated read grant (dropped with the matview); the
-- SECURITY INVOKER portal_health_summary() needs anon to SELECT the matview.
grant select on public.portal_health_mv to anon, authenticated;

-- 3) Repoint the new_listings health check to first_seen_at and add detail_queue_lag.
--    Based on the CURRENT live function body (which already carries the queue +
--    cadence CTEs added after migration 114) — copied verbatim except: a
--    new_listings_fs field on the `m` CTE, the new_listings check status/value/detail,
--    a new `lag` CTE threaded into the final FROM, and the detail_queue_lag check.
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
    jsonb_build_object('key', 'error_rate', 'label', 'Detail-fetch error rate',
      'status', case when coalesce(err_pct, 0) < 5 then 'pass' when coalesce(err_pct, 0) < 15 then 'warn' else 'fail' end,
      'value', coalesce(err_pct, 0)::text || '%',
      'detail', 'Errors as a share of detail work (errors + new + updated) over 24h. Elevated values usually mean the portal is rate-limiting. Warn >5%, fail >15%.'),
    jsonb_build_object('key', 'stale_active', 'label', 'No stale active listings',
      'status', case when stale_active < 50 then 'pass' when stale_active < 500 then 'warn' else 'fail' end,
      'value', stale_active::text,
      'detail', 'Listings still is_active=true but not seen in the index for >7 days — they should have been marked inactive. Warn >50, fail >500.'),
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
from calc, recon, queue, cad, lag;
$function$;

grant execute on function public.scraper_health_checks(text) to anon, authenticated;
