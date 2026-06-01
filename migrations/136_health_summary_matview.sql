-- 136_health_summary_matview.sql
--
-- Back the two heaviest Health-dashboard RPCs with materialized views and
-- repoint the RPCs to read them, so the browser's anon call is a single-row
-- lookup instead of a live multi-scan of the listings table.
--
-- Why: health_summary() rebuilt its whole payload on every page load by
-- scanning listings_public ~10× (255MB heap, 165k rows and growing across
-- portals) plus a GROUP BY over the 300k-row snapshot table. Warm that is
-- ~1.7s; cold-cache, under the anon role's 3s statement_timeout, it failed —
-- the dashboard showed "canceling statement due to statement timeout".
-- portal_health_summary() has the same shape (a full GROUP BY source over
-- listings) at ~1s warm, also at risk cold. A full-table scan on every page
-- load is the wrong shape for a dashboard stat — the data only changes when a
-- scrape runs, while the page polls every 60s.
--
-- So precompute each RPC's exact payload into a single-row matview (id=1,
-- payload jsonb) and have the RPC select that one row (microseconds, same
-- jsonb shape — the frontend contract is byte-identical). This mirrors
-- migration 115 (image_storage_overview_mv); the difference is the refresh
-- driver: an in-DB pg_cron job (refresh_health_matviews, every 10 min) rather
-- than a GitHub Actions script, since the refresh is a pure DB operation with
-- no external inputs and benefits from a tighter-than-hourly cadence. A few
-- minutes' staleness on a health dashboard is invisible. REFRESH ...
-- CONCURRENTLY (needs the unique indexes below) never blocks anon readers.
--
-- The matview is owned by the migration runner and computed off the request
-- path, so anon never executes the scans — it only SELECTs the precomputed
-- row (granted below). No new data is exposed: the payload is the same
-- aggregate counts + failures top-10 the RPC already returned to anon.

------------------------------------------------------------------
-- 1. health_summary_mv  (body copied verbatim from migration 028,
--    wrapped as `select 1 as id, <jsonb> as payload`)
------------------------------------------------------------------

create materialized view if not exists health_summary_mv as
  with
  category_pairs as (
    select * from (values
      ('byt',      'pronajem', 1),
      ('byt',      'prodej',   2),
      ('dum',      'pronajem', 3),
      ('dum',      'prodej',   4),
      ('komercni', 'pronajem', 5),
      ('komercni', 'prodej',   6)
    ) as t(category_main, category_type, sort_order)
  ),
  active_now as (
    select count(*)::int as n
    from listings_public
    where is_active = true
  ),
  active_7d_ago as (
    select count(*)::int as n
    from listings_public
    where first_seen_at <= now() - interval '7 days'
      and (is_active = true or last_seen_at >= now() - interval '7 days')
  ),
  flipped_inactive_7d as (
    select count(*)::int as n
    from listings_public
    where is_active = false
      and last_seen_at >= now() - interval '7 days'
  ),
  last_scrape as (
    select max(last_seen_at) as ts
    from listings_public
  ),
  new_per_day_14 as (
    with series as (
      select generate_series(
        (now() - interval '13 days')::date,
        now()::date,
        '1 day'
      )::date as day
    )
    select
      s.day::text as day,
      coalesce(count(l.sreality_id), 0)::int as n
    from series s
    left join listings_public l
      on date_trunc('day', l.first_seen_at)::date = s.day
    group by s.day
    order by s.day
  ),
  flipped_per_day_7 as (
    with series as (
      select generate_series(
        (now() - interval '6 days')::date,
        now()::date,
        '1 day'
      )::date as day
    )
    select
      s.day::text as day,
      coalesce(count(l.sreality_id), 0)::int as n
    from series s
    left join listings_public l
      on l.is_active = false
     and date_trunc('day', l.last_seen_at)::date = s.day
    group by s.day
    order by s.day
  ),
  snap_density as (
    with counts as (
      select sreality_id, count(*) as snap_count
      from listing_snapshots_public
      group by sreality_id
    )
    select
      case when snap_count >= 4 then '4+' else snap_count::text end as bucket,
      count(*)::int as n
    from counts
    group by 1
  ),
  freshness_24h as (
    select outcome, count(*)::int as n
    from listing_freshness_checks_public
    where checked_at >= now() - interval '24 hours'
    group by outcome
    order by n desc
  ),
  failures_summary as (
    select
      count(*) filter (where given_up = true)::int as given_up,
      count(*)::int                                as total
    from listing_fetch_failures_public
  ),
  failures_top10 as (
    select sreality_id, attempts, first_failure_at, last_failure_at, given_up
    from listing_fetch_failures_public
    order by attempts desc, last_failure_at desc nulls last
    limit 10
  ),

  -- Per-category breakdowns. Each CTE returns one row per pair so the
  -- final jsonb_agg over category_pairs LEFT JOINs cleanly even when a
  -- pair has no listings yet.
  cat_active_now as (
    select l.category_main, l.category_type, count(*)::int as n
    from listings_public l
    where l.is_active = true
    group by l.category_main, l.category_type
  ),
  cat_flipped_7d as (
    select l.category_main, l.category_type, count(*)::int as n
    from listings_public l
    where l.is_active = false
      and l.last_seen_at >= now() - interval '7 days'
    group by l.category_main, l.category_type
  ),
  cat_new_per_day_14 as (
    with series as (
      select generate_series(
        (now() - interval '13 days')::date,
        now()::date,
        '1 day'
      )::date as day
    ),
    counts as (
      select
        l.category_main,
        l.category_type,
        date_trunc('day', l.first_seen_at)::date as day,
        count(*)::int as n
      from listings_public l
      where l.first_seen_at >= (now() - interval '13 days')::date
      group by l.category_main, l.category_type, date_trunc('day', l.first_seen_at)::date
    )
    select
      cp.category_main,
      cp.category_type,
      jsonb_agg(
        jsonb_build_object('day', s.day::text, 'n', coalesce(c.n, 0))
        order by s.day
      ) as series
    from category_pairs cp
    cross join series s
    left join counts c
      on c.category_main = cp.category_main
     and c.category_type = cp.category_type
     and c.day = s.day
    group by cp.category_main, cp.category_type
  ),
  cat_flipped_per_day_7 as (
    with series as (
      select generate_series(
        (now() - interval '6 days')::date,
        now()::date,
        '1 day'
      )::date as day
    ),
    counts as (
      select
        l.category_main,
        l.category_type,
        date_trunc('day', l.last_seen_at)::date as day,
        count(*)::int as n
      from listings_public l
      where l.is_active = false
        and l.last_seen_at >= (now() - interval '6 days')::date
      group by l.category_main, l.category_type, date_trunc('day', l.last_seen_at)::date
    )
    select
      cp.category_main,
      cp.category_type,
      jsonb_agg(
        jsonb_build_object('day', s.day::text, 'n', coalesce(c.n, 0))
        order by s.day
      ) as series
    from category_pairs cp
    cross join series s
    left join counts c
      on c.category_main = cp.category_main
     and c.category_type = cp.category_type
     and c.day = s.day
    group by cp.category_main, cp.category_type
  ),
  cat_failures as (
    select
      l.category_main,
      l.category_type,
      count(*)::int                                  as total,
      count(*) filter (where f.given_up = true)::int as given_up
    from listing_fetch_failures_public f
    join listings_public l on l.sreality_id = f.sreality_id
    group by l.category_main, l.category_type
  ),
  by_category as (
    select
      cp.sort_order,
      jsonb_build_object(
        'category_main',       cp.category_main,
        'category_type',       cp.category_type,
        'active_now',          coalesce(an.n, 0),
        'flipped_inactive_7d', coalesce(f7.n, 0),
        'new_per_day_14d',     coalesce(npd.series, '[]'::jsonb),
        'flipped_per_day_7d',  coalesce(fpd.series, '[]'::jsonb),
        'failures_total',      coalesce(cf.total,    0),
        'failures_given_up',   coalesce(cf.given_up, 0)
      ) as obj
    from category_pairs cp
    left join cat_active_now an
      on an.category_main = cp.category_main and an.category_type = cp.category_type
    left join cat_flipped_7d f7
      on f7.category_main = cp.category_main and f7.category_type = cp.category_type
    left join cat_new_per_day_14 npd
      on npd.category_main = cp.category_main and npd.category_type = cp.category_type
    left join cat_flipped_per_day_7 fpd
      on fpd.category_main = cp.category_main and fpd.category_type = cp.category_type
    left join cat_failures cf
      on cf.category_main = cp.category_main and cf.category_type = cp.category_type
  )

  select 1 as id, jsonb_build_object(
    'last_scrape_at',         (select ts from last_scrape),
    'active_now',             (select n  from active_now),
    'active_7d_ago',          (select n  from active_7d_ago),
    'flipped_inactive_7d',    (select n  from flipped_inactive_7d),
    'new_per_day_14d',        coalesce(
                                (select jsonb_agg(jsonb_build_object('day', day, 'n', n) order by day)
                                 from new_per_day_14),
                                '[]'::jsonb),
    'flipped_per_day_7d',     coalesce(
                                (select jsonb_agg(jsonb_build_object('day', day, 'n', n) order by day)
                                 from flipped_per_day_7),
                                '[]'::jsonb),
    'snapshot_density',       coalesce(
                                (select jsonb_agg(
                                   jsonb_build_object('bucket', bucket, 'n', n)
                                   order by case when bucket = '4+' then 4 else bucket::int end
                                 )
                                 from snap_density),
                                '[]'::jsonb),
    'freshness_24h',          coalesce(
                                (select jsonb_agg(jsonb_build_object('outcome', outcome, 'n', n))
                                 from freshness_24h),
                                '[]'::jsonb),
    'failures_given_up',      (select given_up from failures_summary),
    'failures_total',         (select total    from failures_summary),
    'failures_top10',         coalesce(
                                (select jsonb_agg(jsonb_build_object(
                                   'sreality_id',      sreality_id,
                                   'attempts',         attempts,
                                   'first_failure_at', first_failure_at,
                                   'last_failure_at',  last_failure_at,
                                   'given_up',         given_up
                                 ))
                                 from failures_top10),
                                '[]'::jsonb),
    'by_category',            coalesce(
                                (select jsonb_agg(obj order by sort_order)
                                 from by_category),
                                '[]'::jsonb)
  ) as payload;

create unique index if not exists health_summary_mv_pk on health_summary_mv (id);
grant select on health_summary_mv to anon;

create or replace function health_summary()
returns jsonb
language sql
stable
security invoker
as $$
  select payload from health_summary_mv;
$$;

grant execute on function health_summary() to anon;

------------------------------------------------------------------
-- 2. portal_health_mv  (body copied verbatim from migration 100)
------------------------------------------------------------------

create materialized view if not exists portal_health_mv as
  select 1 as id, coalesce(jsonb_agg(
    jsonb_build_object(
      'source',             p.source,
      'label',              p.label,
      'kind',               p.kind,
      'stage',              p.stage,
      'home_url',           p.home_url,
      'listings_total',     coalesce(lc.listings_total, 0),
      'listings_active',    coalesce(lc.listings_active, 0),
      'listings_active_7d', coalesce(lc.listings_active_7d, 0),
      'parses_total',       coalesce(pa.parses_total, 0),
      'parses_30d',         coalesce(pa.parses_30d, 0),
      'last_scrape_at',     sr.last_run_at,
      'runs_7d',            coalesce(sr.runs_7d, 0),
      'scraped_new_7d',     coalesce(sr.scraped_new_7d, 0),
      'inactive_7d',        coalesce(sr.inactive_7d, 0),
      'errors_7d',          coalesce(sr.errors_7d, 0),
      'last_parsed_at',     pa.last_parsed_at,
      'last_activity_at',   greatest(sr.last_run_at, pa.last_parsed_at, lc.last_seen_at)
    )
    order by p.sort_order, p.label
  ), '[]'::jsonb) as payload
  from portals_public p
  left join portal_listing_counts lc on lc.source = p.source
  left join parsed_url_activity   pa on pa.source = p.source
  left join (
    select
      source,
      max(ended_at) as last_run_at,
      count(*)                       filter (where started_at > now() - interval '7 days') as runs_7d,
      coalesce(sum(listings_scraped_new) filter (where started_at > now() - interval '7 days'), 0) as scraped_new_7d,
      coalesce(sum(listings_inactive)    filter (where started_at > now() - interval '7 days'), 0) as inactive_7d,
      coalesce(sum(errors)               filter (where started_at > now() - interval '7 days'), 0) as errors_7d
    from scrape_runs_public
    group by source
  ) sr on sr.source = p.source
  where p.is_enabled;

create unique index if not exists portal_health_mv_pk on portal_health_mv (id);
grant select on portal_health_mv to anon;

create or replace function portal_health_summary()
returns jsonb
language sql
stable
security invoker
as $$
  select payload from portal_health_mv;
$$;

grant execute on function portal_health_summary() to anon;

------------------------------------------------------------------
-- 3. Refresh driver: pg_cron every 10 min
------------------------------------------------------------------

-- CONCURRENTLY (safe: each matview has the unique index above) so a refresh
-- never blocks the anon readers. SECURITY DEFINER + fixed search_path so the
-- function runs as its owner (the matview owner) regardless of caller.
create or replace function refresh_health_matviews()
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  refresh materialized view concurrently health_summary_mv;
  refresh materialized view concurrently portal_health_mv;
end;
$$;

-- pg_cron schedules the refresh in-DB (every 10 min). Wrapped in a guarded
-- block so this migration still applies on a Postgres without pg_cron — e.g.
-- the CI migration-replay container, where it logs a notice and skips the
-- schedule (the matviews + RPCs above apply everywhere). On Supabase pg_cron is
-- available, so the named job is created — idempotent: re-applying upserts it.
do $cron$
begin
  create extension if not exists pg_cron;
  perform cron.schedule(
    'refresh-health-dashboard',
    '*/10 * * * *',
    $$select public.refresh_health_matviews();$$
  );
exception when others then
  raise notice 'pg_cron unavailable; health matview refresh not scheduled (%). Refresh via refresh_health_matviews() on another scheduler.', sqlerrm;
end
$cron$;
