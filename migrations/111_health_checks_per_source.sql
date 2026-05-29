-- 111_health_checks_per_source.sql
--
-- Generalize scraper_health_checks() to take a source so the Health dashboard
-- can show per-pipeline checks for every scraper portal (sreality, bazos,
-- bezrealitky, …), not just sreality. Same 10 checks, every one now scoped to
-- p_source: the listings-based checks (stale_active, data_freshness,
-- image_coverage, fetch_failures) gain a source filter they previously lacked
-- (they were global = sreality-dominant), and fetch_failures joins listings to
-- resolve a row's source (listing_fetch_failures is keyed by id, not source).
--
-- Default p_source='sreality' keeps the existing no-arg callers working — but
-- the old zero-arg overload (migration 109) must be dropped first, else a
-- no-arg call is ambiguous between f() and f(text default).
--
-- listings_public didn't expose `source` (the listings-based checks were
-- global), so add it — additive trailing column; `source` is already public
-- via properties_public, and create-or-replace preserves the anon grant.

create or replace view listings_public as
 SELECT sreality_id,
    first_seen_at,
    last_seen_at,
    is_active,
    category_main,
    category_type,
    price_czk,
    price_unit,
    area_m2,
    disposition,
    locality,
    district,
    locality_district_id,
    locality_region_id,
    st_y(geom::geometry) AS lat,
    st_x(geom::geometry) AS lng,
    floor,
    total_floors,
    has_balcony,
    has_parking,
    has_lift,
    building_type,
    condition,
    energy_rating,
    estate_area,
    usable_area,
    garden_area,
    category_sub_cb,
    furnished,
    terrace,
    cellar,
    garage,
    parking_lots,
    ownership,
    broker_name,
    broker_email,
    broker_phone,
        CASE
            WHEN is_active THEN GREATEST(0, floor(EXTRACT(epoch FROM now() - first_seen_at) / 86400::numeric)::integer)
            ELSE GREATEST(0, floor(EXTRACT(epoch FROM last_seen_at - first_seen_at) / 86400::numeric)::integer)
        END AS tom_days,
        CASE
            WHEN area_m2 IS NOT NULL AND area_m2 > 0::numeric AND price_czk IS NOT NULL THEN price_czk::numeric / area_m2::numeric
            ELSE NULL::numeric
        END AS price_per_m2,
    building_condition_level,
    apartment_condition_level,
    description,
    source
   FROM listings;

drop function if exists scraper_health_checks();

create or replace function scraper_health_checks(p_source text default 'sreality')
returns jsonb
language sql
stable
security invoker
as $$
with
runs24 as (
  select * from scrape_runs_public
  where started_at > now() - interval '24 hours'
    and source = p_source
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
       join listings_public l on l.sreality_id = f.sreality_id
       where l.source = p_source) as active_fail,
    (select count(*) filter (where f.given_up)
       from listing_fetch_failures_public f
       join listings_public l on l.sreality_id = f.sreality_id
       where l.source = p_source) as given_up,
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
                     when mins_since_start < 90 then 'pass'
                     when mins_since_start < 180 then 'warn' else 'fail' end,
      'value', case when last_start is null then 'never'
                    else coalesce(round(mins_since_start::numeric, 0)::text, '–') || ' min ago' end,
      'detail', 'Last index walk started ' || coalesce(to_char(last_start, 'YYYY-MM-DD HH24:MI'), 'never')
                || ' UTC. GitHub throttles the 15-min schedule, so real cadence is ~hourly. Warn >90 min, fail >180 min.'
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
                     when mins_fresh < 60 then 'pass'
                     when mins_fresh < 180 then 'warn' else 'fail' end,
      'value', case when mins_fresh is null then '–'
                    else coalesce(round(mins_fresh::numeric, 0)::text, '–') || ' min' end,
      'detail', 'Time since the most recently seen active listing. Warn >60 min, fail >180 min.'
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
from calc, recon, queue;
$$;

grant execute on function scraper_health_checks(text) to anon;
