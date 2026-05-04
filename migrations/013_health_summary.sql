-- 013_health_summary.sql
--
-- Aggregates everything the Health dashboard shows in one round-trip.
-- All inputs come from the *_public views established in migration 008,
-- so SECURITY INVOKER is sufficient (anon already has SELECT).
--
-- Counts and time-series are bucketed at the database; the UI never
-- aggregates rows itself. Should run in well under 500 ms at current
-- volumes (~10k listings, ~10k snapshots, ~1k failures).
--
-- "Active 7 days ago" and "flipped inactive in last 7 days" are
-- inferred — there's no deactivated_at column. The proxies:
--   active 7d ago    = first_seen_at <= now()-7d
--                      AND (is_active OR last_seen_at >= now()-7d)
--   flipped inactive = is_active = false
--                      AND last_seen_at >= now()-7d
-- Imperfect at the edges (a listing last seen 8 days ago that was
-- flipped today is missed) but good enough for the operator dashboard.

create or replace function health_summary()
returns jsonb
language sql
stable
security invoker
as $$
  with
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
  -- Densify with generate_series so the chart always has 14 data points,
  -- including any days with zero new listings.
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
  -- Same densification for the inactive-flip sparkline.
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
                                '[]'::jsonb)
  );
$$;

grant execute on function health_summary() to anon;
