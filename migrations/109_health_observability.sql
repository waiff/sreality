-- 109_health_observability.sql
--
-- Make the Health dashboard tell the truth about the post-split pipeline.
-- Three additive changes, all SECURITY INVOKER / anon-readable like the rest
-- of the Health surface:
--
--   1. image_storage_overview() — add active-listing image counts (global +
--      per category) alongside the all-listings counts. The active gap is the
--      *closeable* one (the CDN still serves those photos); inactive-listing
--      photos are mostly expired and unrecoverable, so conflating them hid the
--      real coverage target.
--
--   2. listing_detail_queue_public — a read-only projection of the needs-detail
--      queue (migration 105) so the dashboard can show the detail-drain backlog.
--      Base table has RLS + no anon policy; the view (owner's rights, like every
--      other *_public view) exposes only non-sensitive columns to anon.
--
--   3. scraper_health_checks() — redefine (from migration 105). The split makes
--      the old count_reconciliation check misleading: it compared our *active*
--      count to sreality's result_size, but a new listing only becomes an active
--      row once the detail-drain fetches it. So the "drift" it screamed was just
--      the un-drained backlog, not data loss. Split that into two honest checks:
--        * index_completeness  — did the index walk SEE every listing
--          (collected vs result_size). ~0% on a healthy walk.
--        * detail_queue_backlog — how many seen-but-not-yet-fetched listings sit
--          in the queue. THIS is the real lag behind the apparent drift.

-- 1. Image mirror: active-only counts ---------------------------------------

create or replace function image_storage_overview()
returns jsonb
language sql
stable
security invoker
as $$
  with per_cat as (
    select
      l.category_main,
      l.category_type,
      count(i.id)                                       as total,
      count(i.storage_path)                             as stored,
      count(i.id) filter (where l.is_active)            as total_active,
      count(i.storage_path) filter (where l.is_active)  as stored_active
    from listings_public l
    left join images_public i on i.sreality_id = l.sreality_id
    group by 1, 2
  ),
  active_totals as (
    select
      count(i.id)            as total_active,
      count(i.storage_path)  as stored_active
    from listings_public l
    join images_public i on i.sreality_id = l.sreality_id
    where l.is_active
  )
  select jsonb_build_object(
    'total_images',         (select count(*) from images_public),
    'stored_images',        (select count(*) from images_public where storage_path is not null),
    'total_active_images',  (select total_active from active_totals),
    'stored_active_images', (select stored_active from active_totals),
    'by_category', coalesce((
      select jsonb_agg(
        jsonb_build_object(
          'category_main', category_main,
          'category_type', category_type,
          'total',         total,
          'stored',        stored,
          'total_active',  total_active,
          'stored_active', stored_active
        )
        order by category_main, category_type
      )
      from per_cat
    ), '[]'::jsonb)
  )
$$;
grant execute on function image_storage_overview() to anon;

-- 2. Detail-drain backlog: anon-readable projection of the internal queue ----

create or replace view listing_detail_queue_public as
  select source, priority, enqueued_at, claimed_at, given_up
  from listing_detail_queue;
grant select on listing_detail_queue_public to anon;

-- 3. Health checks: split apparent "drift" into index-completeness + backlog -

create or replace function scraper_health_checks()
returns jsonb
language sql
stable
security invoker
as $$
with
runs24 as (
  select * from scrape_runs_public
  where started_at > now() - interval '24 hours'
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
queue as (
  select
    count(*) filter (where claimed_at is null and not given_up) as claimable,
    count(*) filter (where claimed_at is null and not given_up and priority = 1) as changed,
    count(*) filter (where given_up) as given_up
  from listing_detail_queue_public
  where source = 'sreality'
),
recon as (
  -- Index completeness: did we SEE every listing (collected vs sreality's
  -- result_size). Distinct from the detail backlog (have we FETCHED them).
  select
    count(*) as n_with_data,
    max(gap_pct) as max_gap_pct
  from (
    select abs((e->>'collected')::numeric - (e->>'sreality_result_size')::numeric)
             / nullif((e->>'sreality_result_size')::numeric, 0) * 100.0 as gap_pct
    from (
      select by_category from scrape_runs_public
      where ended_at is not null and index_pages > 0 and source = 'sreality'
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
  'checks', jsonb_build_array(
    jsonb_build_object(
      'key', 'liveness', 'label', 'Scraper running on schedule',
      'status', case when mins_since_start < 90 then 'pass'
                     when mins_since_start < 180 then 'warn' else 'fail' end,
      'value', coalesce(round(mins_since_start::numeric, 0)::text, '–') || ' min ago',
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
      'status', case when mins_fresh < 60 then 'pass'
                     when mins_fresh < 180 then 'warn' else 'fail' end,
      'value', coalesce(round(mins_fresh::numeric, 0)::text, '–') || ' min',
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
                || 'sreality''s reported result_size on the latest completed index walk — i.e. did the '
                || 'walk SEE every listing. Whether we have FETCHED them is the separate detail-drain '
                || 'backlog. Populates once the region-split walk records per-category result_size. '
                || 'Warn >2%, fail >5%.'
    )
  )
)
from calc, recon, queue;
$$;

grant execute on function scraper_health_checks() to anon;
