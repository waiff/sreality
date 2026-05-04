-- 021_region_stats_box.sql
--
-- Extends region_stats() with per-disposition box-plot data on
-- price-per-m² so browse-2 can replace the disposition summary table
-- with one box plot per disposition.
--
-- Adds a `ppm2_box` jsonb to each entry of the `dispositions` array:
--   { "n", "min", "p25", "median", "p75", "max" }
--
-- `ppm2_box` is null when the disposition has zero listings with both
-- price_czk and area_m2 > 0 (the existing per-disposition row may
-- still be present if there are listings without price/area).
--
-- The other dispositions fields (median_price, median_ppm2,
-- median_area) are preserved so the browse-1 DispositionTable keeps
-- rendering between this migration landing and step 8 (C3) replacing
-- the table with the box-plot component.
--
-- This is a CREATE OR REPLACE on an existing function — the signature
-- and grant are unchanged from migration 012.

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
      percentile_cont(0.50) within group (order by area_m2)::int as median_area,
      count(price_czk::numeric / nullif(area_m2, 0))::int as ppm2_n,
      min(price_czk::numeric / nullif(area_m2, 0))::int as ppm2_min,
      percentile_cont(0.25) within group (
        order by price_czk::numeric / nullif(area_m2, 0)
      )::int as ppm2_p25,
      percentile_cont(0.75) within group (
        order by price_czk::numeric / nullif(area_m2, 0)
      )::int as ppm2_p75,
      max(price_czk::numeric / nullif(area_m2, 0))::int as ppm2_max
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
                                  'median_area',   median_area,
                                  'ppm2_box',      case when ppm2_n > 0 then
                                                     jsonb_build_object(
                                                       'n',      ppm2_n,
                                                       'min',    ppm2_min,
                                                       'p25',    ppm2_p25,
                                                       'median', median_ppm2,
                                                       'p75',    ppm2_p75,
                                                       'max',    ppm2_max
                                                     )
                                                   else null end
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
