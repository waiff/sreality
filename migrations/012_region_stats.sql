-- 012_region_stats.sql
--
-- RPCs feeding the U1a Region page (/region). Two functions:
--
--   region_stats(...)         -> single jsonb (headline + percentiles +
--                                per-disposition + time-on-market)
--   region_active_by_day(...) -> setof (day date, active int, new int)
--                                for the last N days (default 90)
--
-- Region selection is one of two modes:
--   * District mode: districts_filter is a non-empty text[].
--   * Radius mode:   center_lng + center_lat + radius_m all non-null.
-- If both are given the district filter wins (union not supported).
-- If neither is given the function returns no rows / a null payload.
--
-- Both functions are SECURITY INVOKER and read listings_public, so they
-- inherit anon's existing SELECT grant from migration 008. The radius
-- predicate reconstructs geography from the lat/lng columns already
-- exposed by the public view (geom itself is never sent across the
-- function boundary). At ~10k rows this runs as a seq scan in ~50ms;
-- the cost grows linearly and is acceptable up to ~100k.
--
-- Both functions return only aggregates; no row-level identifiers leak
-- beyond what listings_public already exposes.

create or replace function region_stats(
  districts_filter text[]           default null,
  center_lng       double precision default null,
  center_lat       double precision default null,
  radius_m         integer          default null
)
returns jsonb
language sql
stable
security invoker
as $$
  with filtered as (
    select
      first_seen_at, last_seen_at, is_active,
      price_czk, area_m2, disposition
    from listings_public
    where
      case
        when districts_filter is not null and array_length(districts_filter, 1) > 0 then
          district = any(districts_filter)
        when center_lng is not null and center_lat is not null and radius_m is not null then
          lat is not null and lng is not null
          and ST_DWithin(
                ST_SetSRID(ST_MakePoint(lng, lat), 4326)::geography,
                ST_SetSRID(ST_MakePoint(center_lng, center_lat), 4326)::geography,
                radius_m
              )
        else false
      end
  ),
  active_only as (
    select * from filtered where is_active = true
  ),
  price_pct as (
    select
      percentile_cont(0.25) within group (order by price_czk)::int as p25,
      percentile_cont(0.50) within group (order by price_czk)::int as p50,
      percentile_cont(0.75) within group (order by price_czk)::int as p75
    from active_only
    where price_czk is not null
  ),
  ppm2_pct as (
    select
      percentile_cont(0.25) within group (order by price_czk::numeric / area_m2)::int as p25,
      percentile_cont(0.50) within group (order by price_czk::numeric / area_m2)::int as p50,
      percentile_cont(0.75) within group (order by price_czk::numeric / area_m2)::int as p75
    from active_only
    where price_czk is not null and area_m2 is not null and area_m2 > 0
  ),
  disposition_dist as (
    select
      coalesce(disposition, 'unspecified') as disposition,
      count(*)::int as n,
      percentile_cont(0.50) within group (order by price_czk)::int as median_price,
      percentile_cont(0.50) within group (
        order by price_czk::numeric / nullif(area_m2, 0)
      )::int as median_ppm2,
      percentile_cont(0.50) within group (order by area_m2)::int as median_area
    from active_only
    group by disposition
    order by n desc, disposition asc
  ),
  delisted as (
    select
      extract(epoch from (last_seen_at - first_seen_at)) / 86400.0 as days_alive
    from filtered
    where is_active = false
  ),
  tom as (
    select
      count(*)::int as n,
      percentile_cont(0.50) within group (order by days_alive)::numeric(10,1) as median_days
    from delisted
  )
  select jsonb_build_object(
    'total_active',          (select count(*)::int from active_only),
    'total_ever',            (select count(*)::int from filtered),
    'last_new_first_seen',   (select max(first_seen_at) from filtered),
    'price',                 (select case when p50 is not null
                                          then jsonb_build_object('p25', p25, 'p50', p50, 'p75', p75)
                                          else null end
                              from price_pct),
    'ppm2',                  (select case when p50 is not null
                                          then jsonb_build_object('p25', p25, 'p50', p50, 'p75', p75)
                                          else null end
                              from ppm2_pct),
    'dispositions',          coalesce(
                               (select jsonb_agg(jsonb_build_object(
                                  'disposition',   disposition,
                                  'n',             n,
                                  'median_price',  median_price,
                                  'median_ppm2',   median_ppm2,
                                  'median_area',   median_area
                                )) from disposition_dist),
                               '[]'::jsonb
                             ),
    'tom_median_days',       (select median_days from tom),
    'tom_n',                 (select n from tom)
  );
$$;

grant execute on function region_stats(
  text[], double precision, double precision, integer
) to anon;


create or replace function region_active_by_day(
  districts_filter text[]           default null,
  center_lng       double precision default null,
  center_lat       double precision default null,
  radius_m         integer          default null,
  days_back        integer          default 90
)
returns table(day date, active int, new int)
language sql
stable
security invoker
as $$
  with filtered as (
    select first_seen_at, last_seen_at, is_active
    from listings_public
    where
      case
        when districts_filter is not null and array_length(districts_filter, 1) > 0 then
          district = any(districts_filter)
        when center_lng is not null and center_lat is not null and radius_m is not null then
          lat is not null and lng is not null
          and ST_DWithin(
                ST_SetSRID(ST_MakePoint(lng, lat), 4326)::geography,
                ST_SetSRID(ST_MakePoint(center_lng, center_lat), 4326)::geography,
                radius_m
              )
        else false
      end
  )
  select
    d.day::date,
    count(*) filter (
      where f.first_seen_at < (d.day + interval '1 day')
        and (f.is_active or f.last_seen_at >= d.day)
    )::int as active,
    count(*) filter (
      where f.first_seen_at >= d.day
        and f.first_seen_at <  (d.day + interval '1 day')
    )::int as new
  from generate_series(
         (now() at time zone 'UTC')::date - (days_back - 1),
         (now() at time zone 'UTC')::date,
         interval '1 day'
       ) as d(day)
  left join filtered f on true
  group by d.day
  order by d.day;
$$;

grant execute on function region_active_by_day(
  text[], double precision, double precision, integer, integer
) to anon;
