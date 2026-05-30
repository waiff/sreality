-- 114_portal_cadence_health_thresholds.sql
--
-- Make the per-portal "Scraper running on schedule" (liveness) and "Data
-- freshness" health checks CADENCE-AWARE. Their thresholds were hardcoded
-- (liveness warn>90/fail>180 min; freshness warn>60/fail>180 min), tuned for
-- sreality's ~hourly real cadence. A 6-hourly portal (bazos / bezrealitky /
-- idnes) therefore sat in PROBLEM between runs purely as an artefact of the
-- mismatch — the data was fine. Now each portal carries its expected cadence
-- and the two time-since checks scale off it.
--
-- Calibration (so sreality is BYTE-IDENTICAL to before): with cadence = 60,
--   liveness  pass < 1.5*cad (90),  warn < 3*cad (180)  -> 90/180 as today
--   freshness pass < 1.0*cad (60),  warn < 3*cad (180)  -> 60/180 as today
-- A 6h portal (cadence 360) gets pass<540/warn<1080 (liveness) and
-- pass<360/warn<1080 (freshness) — green between its normal runs.
--
-- Purely additive: a new nullable column (+ backfill), a view column append,
-- and a CREATE OR REPLACE of the existing function. NULL cadence falls back to
-- 60 inside the function, so an un-backfilled / parser-only portal is unchanged.

alter table portals add column scrape_cadence_minutes integer;

-- sreality is the */15 split index_walk/detail_drain, throttled by GitHub to
-- ~hourly real cadence (60). The HTML/GraphQL pilots run every 6h (360).
update portals set scrape_cadence_minutes = 60  where source = 'sreality';
update portals set scrape_cadence_minutes = 360 where source in ('bazos', 'bezrealitky', 'idnes');

-- Expose the new column on the anon-readable registry view (the SECURITY INVOKER
-- health function reads cadence from here). CREATE OR REPLACE appends the column.
create or replace view portals_public as
  select source, label, kind, stage, home_url, sort_order, is_enabled,
         supports_complete_walk, categories, split_threshold, scrape_cadence_minutes
  from portals;
grant select on portals_public to anon;

create or replace function public.scraper_health_checks(p_source text default 'sreality'::text)
 returns jsonb
 language sql
 stable
as $function$
with
runs24 as (
  select * from scrape_runs_public
  where started_at > now() - interval '24 hours'
    and source = p_source
),
cad as (
  -- expected cadence (minutes) for this portal; NULL/unknown -> 60 (sreality-like)
  select coalesce((select scrape_cadence_minutes from portals_public where source = p_source), 60) as mins
),
m as (
  select
    extract(epoch from now() - (select max(started_at) from scrape_runs_public where index_pages > 0 and source = p_source))/60.0 as mins_since_start,
    (select max(started_at) from scrape_runs_public where index_pages > 0 and source = p_source) as last_start,
    (select count(*) from scrape_runs_public
       where ended_at is null and started_at < now() - interval '30 minutes'
         and started_at > now() - interval '6 hours'
         and source = p_source) as stuck,
    coalesce((select sum(listings_scraped_new) from runs24), 0) as scraped_new,
    coalesce((select sum(listings_updated) from runs24), 0) as updated,
    coalesce((select max(listings_inactive) from runs24), 0) as inactive_max,
    coalesce((select sum(errors) from runs24), 0) as errors_sum,
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
       from images_public i
       join listings_public l on l.sreality_id = i.sreality_id
       where l.is_active and l.source = p_source) as img_pct
),
calc as (
  select *,
    round(100.0 * errors_sum / nullif(errors_sum + scraped_new + updated, 0), 1) as err_pct
  from m
),
queue as (
  select
    count(*) filter (where claimed_at is null and not given_up) as claimable,
    count(*) filter (where claimed_at is null and not given_up and priority = 1) as changed,
    count(*) filter (where given_up) as given_up
  from listing_detail_queue_public
  where source = p_source
),
recon as (
  select
    count(*) as n_with_data,
    max(gap_pct) as max_gap_pct
  from (
    select abs((e->>'collected')::numeric - (e->>'sreality_result_size')::numeric)
             / nullif((e->>'sreality_result_size')::numeric, 0) * 100.0 as gap_pct
    from (
      select by_category from scrape_runs_public
      where ended_at is not null and index_pages > 0 and source = p_source
      order by started_at desc
      limit 1
    ) latest,
    lateral jsonb_array_elements(coalesce(latest.by_category, '[]'::jsonb)) e
    where (e->>'sreality_result_size') is not null
      and (e->>'collected') is not null
      and (e->>'sreality_result_size')::numeric > 0
  ) d
)
select jsonb_build_object(
  'generated_at', now(),
  'source', p_source,
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
                || 'Warn >' || round(cad.mins * 1.5)::text || ' min, fail >' || round(cad.mins * 3)::text || ' min.'
    ),
    jsonb_build_object(
      'key', 'runs_completing', 'label', 'Runs finishing cleanly',
      'status', case when stuck = 0 then 'pass' when stuck = 1 then 'warn' else 'fail' end,
      'value', stuck::text || ' stuck',
      'detail', 'Index-walk or detail-drain runs started >30 min ago (last 6h) that never recorded an end timestamp — a crash or timeout before finalize. Expected 0.'
    ),
    jsonb_build_object(
      'key', 'new_listings', 'label', 'New listings flowing',
      'status', case when scraped_new > 0 then 'pass' else 'warn' end,
      'value', scraped_new::text || ' / 24h',
      'detail', 'New listings written by the detail-drain in the last 24h. 0 over a full day suggests the index-walk enqueue or the detail-drain is blocked.'
    ),
    jsonb_build_object(
      'key', 'delisting_spike', 'label', 'No false mass-delisting',
      'status', case when inactive_max <= 500 then 'pass'
                     when inactive_max <= 2000 then 'warn' else 'fail' end,
      'value', inactive_max::text || ' max/run',
      'detail', 'Largest single-run inactivation in 24h (the index-walk''s mark_inactive). A big spike usually means a truncated index walk falsely delisted live listings; the walk-completeness guard mitigates this. Warn >500, fail >2000.'
    ),
    jsonb_build_object(
      'key', 'error_rate', 'label', 'Detail-fetch error rate',
      'status', case when coalesce(err_pct, 0) < 5 then 'pass'
                     when coalesce(err_pct, 0) < 15 then 'warn' else 'fail' end,
      'value', coalesce(err_pct, 0)::text || '%',
      'detail', 'Errors as a share of detail work (errors + new + updated) over 24h. Elevated values usually mean the portal is rate-limiting. Warn >5%, fail >15%.'
    ),
    jsonb_build_object(
      'key', 'stale_active', 'label', 'No stale active listings',
      'status', case when stale_active < 50 then 'pass'
                     when stale_active < 500 then 'warn' else 'fail' end,
      'value', stale_active::text,
      'detail', 'Listings still is_active=true but not seen in the index for >7 days — they should have been marked inactive. Warn >50, fail >500.'
    ),
    jsonb_build_object(
      'key', 'fetch_failures', 'label', 'Fetch-failure backlog',
      'status', case when active_fail < 1000 then 'pass'
                     when active_fail < 5000 then 'warn' else 'fail' end,
      'value', active_fail::text || ' active',
      'detail', calc.given_up::text || ' listings given up after repeated failures. Active failures retry with priority next run. Warn >1000, fail >5000.'
    ),
    jsonb_build_object(
      'key', 'detail_queue_backlog', 'label', 'Detail-drain backlog',
      'status', case when queue.claimable < 2000 then 'pass'
                     when queue.claimable < 10000 then 'warn' else 'fail' end,
      'value', queue.claimable::text || ' queued',
      'detail', 'New + price-changed listings the index walk enqueued but the detail-drain '
                || 'has not fetched yet (' || queue.changed::text || ' price-changed). A new listing '
                || 'becomes an active row only once drained, so THIS backlog — not data loss — is what '
                || 'opens the gap in "Index walk completeness". The drain closes it; raise its cap/cadence '
                || 'if it grows. ' || queue.given_up::text || ' given up. Warn >2k, fail >10k.'
    ),
    jsonb_build_object(
      'key', 'data_freshness', 'label', 'Data freshness',
      'status', case when mins_fresh is null then 'warn'
                     when mins_fresh < cad.mins then 'pass'
                     when mins_fresh < cad.mins * 3 then 'warn' else 'fail' end,
      'value', case when mins_fresh is null then '–'
                    else coalesce(round(mins_fresh::numeric, 0)::text, '–') || ' min' end,
      'detail', 'Time since the most recently seen active listing. '
                || 'Warn >' || cad.mins::text || ' min, fail >' || round(cad.mins * 3)::text || ' min.'
    ),
    jsonb_build_object(
      'key', 'index_completeness', 'label', 'Index walk completeness',
      'status', case
                  when recon.n_with_data = 0 then 'warn'
                  when coalesce(recon.max_gap_pct, 0) < 2 then 'pass'
                  when coalesce(recon.max_gap_pct, 0) < 5 then 'warn'
                  else 'fail' end,
      'value', case when recon.n_with_data = 0 then 'no data yet'
                    else round(coalesce(recon.max_gap_pct, 0), 1)::text || '% max gap' end,
      'detail', 'Largest per-category gap between how many index entries we collected and '
                || 'the portal''s reported result_size on the latest completed index walk — i.e. did the '
                || 'walk SEE every listing. Whether we have FETCHED them is the separate detail-drain '
                || 'backlog. Populates once the walk records per-category result_size. Warn >2%, fail >5%.'
    )
  )
)
from calc, recon, queue, cad;
$function$;
