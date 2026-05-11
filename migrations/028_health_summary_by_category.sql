-- 028_health_summary_by_category.sql
--
-- Extends the health_summary() RPC (originally migration 013) with a
-- per-category breakdown. Each entry in `by_category` carries the same
-- metrics that used to be global-only — active count, new-per-day for
-- the last 14 days, flipped-inactive count and 7-day sparkline, and
-- fetch-failure counts joined back through listings_public.
--
-- The category list is fixed to the six (category_main, category_type)
-- pairs that the scraper walks (see scraper/main.py CATEGORIES). Stray
-- legacy values like `drazba` / `podil` exist in the DB but are out of
-- product scope and would clutter the tile grid, so we hard-pin the
-- pairs in a CTE rather than `GROUP BY` over whatever the data
-- contains. Order is the same as in the scraper for visual stability.
--
-- The pre-existing top-level fields are preserved so older clients
-- keep working; new clients read `by_category`.

create or replace function health_summary()
returns jsonb
language sql
stable
security invoker
as $$
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

  select jsonb_build_object(
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
  );
$$;

grant execute on function health_summary() to anon;
