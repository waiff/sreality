-- 101_health_checks_sreality_source.sql
-- The bazos pilot crawler (bazos_main.py) now writes scrape_runs rows with
-- source='bazos' and index_pages>0, on its own schedule. scraper_health_checks()
-- (migration 090) keys liveness + reconciliation + the 24h counters off
-- scrape_runs WITHOUT a source filter, so a bazos run becomes "the latest run"
-- and corrupts the sreality panel — masking a sreality outage in liveness and
-- blanking the count-reconciliation. This panel is sreality-specific (the
-- frontend labels it "· sreality pipeline"; per-portal health lives in
-- portal_health_summary from migration 100), so scope every scrape_runs read to
-- source='sreality'. The listings/images-based checks are left as-is: bazos has
-- ingested no listings yet, so they are already sreality-only in practice.
-- Redefines scraper_health_checks() from migration 090.

create or replace function scraper_health_checks()
returns jsonb
language sql
stable
security invoker
as $$
with
runs24 as (
  select * from scrape_runs_public
  where started_at > now() - interval '24 hours' and index_pages > 0
    and source = 'sreality'
),
m as (
  select
    extract(epoch from now() - (select max(started_at) from scrape_runs_public where index_pages > 0 and source = 'sreality'))/60.0 as mins_since_start,
    (select max(started_at) from scrape_runs_public where index_pages > 0 and source = 'sreality') as last_start,
    (select count(*) from scrape_runs_public
       where ended_at is null and started_at < now() - interval '30 minutes'
         and started_at > now() - interval '6 hours'
         and source = 'sreality') as stuck,
    coalesce((select sum(listings_scraped_new) from runs24), 0) as scraped_new,
    coalesce((select sum(listings_updated) from runs24), 0) as updated,
    coalesce((select max(listings_inactive) from runs24), 0) as inactive_max,
    coalesce((select sum(errors) from runs24), 0) as errors_sum,
    (select count(*) filter (where is_active and last_seen_at < now() - interval '7 days')
       from listings_public) as stale_active,
    extract(epoch from now() - (select max(last_seen_at) from listings_public where is_active))/60.0 as mins_fresh,
    (select count(*) filter (where not given_up) from listing_fetch_failures_public) as active_fail,
    (select count(*) filter (where given_up) from listing_fetch_failures_public) as given_up,
    (select round(100.0 * count(*) filter (where i.storage_path is not null) / nullif(count(*), 0), 1)
       from images_public i
       join listings_public l on l.sreality_id = i.sreality_id
       where l.is_active) as img_pct
),
calc as (
  select *,
    round(100.0 * errors_sum / nullif(errors_sum + scraped_new + updated, 0), 1) as err_pct
  from m
),
recon as (
  select
    count(*) as n_with_data,
    max(drift_pct) as max_drift_pct
  from (
    select abs((e->>'active_db')::numeric - (e->>'sreality_result_size')::numeric)
             / nullif((e->>'sreality_result_size')::numeric, 0) * 100.0 as drift_pct
    from (
      select by_category from scrape_runs_public
      where ended_at is not null and index_pages > 0 and source = 'sreality'
      order by started_at desc
      limit 1
    ) latest,
    lateral jsonb_array_elements(coalesce(latest.by_category, '[]'::jsonb)) e
    where (e->>'sreality_result_size') is not null
      and (e->>'active_db') is not null
      and (e->>'sreality_result_size')::numeric > 0
  ) d
)
select jsonb_build_object(
  'generated_at', now(),
  'checks', jsonb_build_array(
    jsonb_build_object(
      'key', 'liveness', 'label', 'Scraper running on schedule',
      'status', case when mins_since_start < 90 then 'pass'
                     when mins_since_start < 180 then 'warn' else 'fail' end,
      'value', coalesce(round(mins_since_start::numeric, 0)::text, '–') || ' min ago',
      'detail', 'Last completed index walk started ' || coalesce(to_char(last_start, 'YYYY-MM-DD HH24:MI'), 'never')
                || ' UTC. GitHub throttles the 15-min schedule, so real cadence is ~hourly. Warn >90 min, fail >180 min.'
    ),
    jsonb_build_object(
      'key', 'runs_completing', 'label', 'Runs finishing cleanly',
      'status', case when stuck = 0 then 'pass' when stuck = 1 then 'warn' else 'fail' end,
      'value', stuck::text || ' stuck',
      'detail', 'Runs started >30 min ago (last 6h) that never recorded an end timestamp — a crash or timeout before finalize. Expected 0.'
    ),
    jsonb_build_object(
      'key', 'new_listings', 'label', 'New listings flowing',
      'status', case when scraped_new > 0 then 'pass' else 'warn' end,
      'value', scraped_new::text || ' / 24h',
      'detail', 'New listings inserted in the last 24h. 0 over a full day suggests the index walk is blocked or returning empty.'
    ),
    jsonb_build_object(
      'key', 'delisting_spike', 'label', 'No false mass-delisting',
      'status', case when inactive_max <= 500 then 'pass'
                     when inactive_max <= 2000 then 'warn' else 'fail' end,
      'value', inactive_max::text || ' max/run',
      'detail', 'Largest single-run inactivation in 24h. A big spike usually means a truncated index walk falsely delisted live listings; the walk-completeness guard mitigates this. Warn >500, fail >2000.'
    ),
    jsonb_build_object(
      'key', 'error_rate', 'label', 'Detail-fetch error rate',
      'status', case when coalesce(err_pct, 0) < 5 then 'pass'
                     when coalesce(err_pct, 0) < 15 then 'warn' else 'fail' end,
      'value', coalesce(err_pct, 0)::text || '%',
      'detail', 'Errors as a share of detail work (errors + new + updated) over 24h. Elevated values usually mean sreality is rate-limiting. Warn >5%, fail >15%.'
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
      'detail', given_up::text || ' listings given up after repeated failures. Active failures retry with priority next run. Warn >1000, fail >5000.'
    ),
    jsonb_build_object(
      'key', 'image_coverage', 'label', 'Image mirror coverage (active)',
      'status', case when coalesce(img_pct, 0) >= 80 then 'pass'
                     when coalesce(img_pct, 0) >= 40 then 'warn' else 'fail' end,
      'value', coalesce(img_pct, 0)::text || '%',
      'detail', 'Share of active-listing images downloaded to R2. The 2-hourly backfill climbs this toward full coverage. Warn <80%, fail <40%.'
    ),
    jsonb_build_object(
      'key', 'data_freshness', 'label', 'Data freshness',
      'status', case when mins_fresh < 60 then 'pass'
                     when mins_fresh < 180 then 'warn' else 'fail' end,
      'value', coalesce(round(mins_fresh::numeric, 0)::text, '–') || ' min',
      'detail', 'Time since the most recently seen active listing. Warn >60 min, fail >180 min.'
    ),
    jsonb_build_object(
      'key', 'count_reconciliation', 'label', 'Listing count vs sreality',
      'status', case
                  when recon.n_with_data = 0 then 'warn'
                  when coalesce(recon.max_drift_pct, 0) < 2 then 'pass'
                  when coalesce(recon.max_drift_pct, 0) < 5 then 'warn'
                  else 'fail' end,
      'value', case when recon.n_with_data = 0 then 'no data yet'
                    else round(coalesce(recon.max_drift_pct, 0), 1)::text || '% max drift' end,
      'detail', 'Largest per-category gap between our active count and sreality''s '
                || 'reported result_size on the latest completed index walk. Populates once the '
                || 'region-split scraper records per-category result_size. Warn >2%, fail >5%.'
    )
  )
)
from calc, recon;
$$;

grant execute on function scraper_health_checks() to anon;
