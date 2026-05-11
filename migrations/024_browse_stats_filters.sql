-- 024_browse_stats_filters.sql
-- Extend browse_stats() and region_stats() to filter on the new columns
-- added in migration 022:
--   browse_stats: furnished_filter (text), terrace_filter (bool),
--                 cellar_filter (bool), garage_filter (bool),
--                 category_sub_cb_filter (int).
--   region_stats: category_sub_cb_filter (int).
--
-- DROP-then-CREATE because PostgreSQL identifies functions by full
-- argument signature; adding a parameter creates a new overload, and
-- the SPA's existing call sites pass arguments by name so we want a
-- single canonical signature, not two.
--
-- New params default to NULL so any caller written against the
-- previous signature keeps working unchanged. The frontend in PR B
-- starts passing them.

drop function if exists browse_stats(
  text[], text[], integer, integer, integer, integer,
  boolean, integer, boolean, boolean, boolean, boolean
);

create or replace function browse_stats(
  districts_filter         text[]   default null,
  dispositions_filter      text[]   default null,
  price_min_filter         integer  default null,
  price_max_filter         integer  default null,
  area_min_filter          integer  default null,
  area_max_filter          integer  default null,
  active_only_filter       boolean  default true,
  seen_within_days_filter  integer  default 7,
  has_balcony_filter       boolean  default null,
  has_lift_filter          boolean  default null,
  has_parking_filter       boolean  default null,
  inactive_only_filter     boolean  default false,
  furnished_filter         text     default null,
  terrace_filter           boolean  default null,
  cellar_filter            boolean  default null,
  garage_filter            boolean  default null,
  category_sub_cb_filter   integer  default null
)
returns jsonb
language sql
stable
security invoker
as $$
  with filtered as (
    select
      sreality_id, first_seen_at, last_seen_at, is_active,
      price_czk, area_m2, disposition
    from listings_public
    where
          (not active_only_filter   or is_active = true)
      and (not inactive_only_filter or is_active = false)
      and (seen_within_days_filter is null
           or last_seen_at >= now() - (seen_within_days_filter || ' days')::interval)
      and (districts_filter      is null or district       = any(districts_filter))
      and (dispositions_filter   is null or disposition    = any(dispositions_filter))
      and (price_min_filter      is null or price_czk     >= price_min_filter)
      and (price_max_filter      is null or price_czk     <= price_max_filter)
      and (area_min_filter       is null or area_m2       >= area_min_filter)
      and (area_max_filter       is null or area_m2       <= area_max_filter)
      and (has_balcony_filter    is null or has_balcony    = has_balcony_filter)
      and (has_lift_filter       is null or has_lift       = has_lift_filter)
      and (has_parking_filter    is null or has_parking    = has_parking_filter)
      and (furnished_filter      is null or furnished      = furnished_filter)
      and (terrace_filter        is null or terrace        = terrace_filter)
      and (cellar_filter         is null or cellar         = cellar_filter)
      and (garage_filter         is null or garage         = garage_filter)
      and (category_sub_cb_filter is null or category_sub_cb = category_sub_cb_filter)
  ),
  price_pct as (
    select
      percentile_cont(0.25) within group (order by price_czk)::int as p25,
      percentile_cont(0.50) within group (order by price_czk)::int as p50,
      percentile_cont(0.75) within group (order by price_czk)::int as p75
    from filtered
    where price_czk is not null
  ),
  ppm2_pct as (
    select
      percentile_cont(0.25) within group (order by price_czk::numeric / area_m2)::int as p25,
      percentile_cont(0.50) within group (order by price_czk::numeric / area_m2)::int as p50,
      percentile_cont(0.75) within group (order by price_czk::numeric / area_m2)::int as p75
    from filtered
    where price_czk is not null and area_m2 is not null and area_m2 > 0
  ),
  disposition_dist as (
    select
      coalesce(disposition, 'unspecified') as disposition,
      count(*)::int as n
    from filtered
    group by disposition
    order by n desc, disposition asc
  )
  select jsonb_build_object(
    'total',        (select count(*)::int from filtered),
    'new_7d',       (select count(*)::int from filtered where first_seen_at >= now() - interval '7 days'),
    'new_30d',      (select count(*)::int from filtered where first_seen_at >= now() - interval '30 days'),
    'price',        (select case when p50 is null then null
                                  else jsonb_build_object('p25', p25, 'p50', p50, 'p75', p75) end
                     from price_pct),
    'ppm2',         (select case when p50 is null then null
                                  else jsonb_build_object('p25', p25, 'p50', p50, 'p75', p75) end
                     from ppm2_pct),
    'dispositions', coalesce(
                      (select jsonb_agg(jsonb_build_object('disposition', disposition, 'n', n)) from disposition_dist),
                      '[]'::jsonb
                    )
  );
$$;

grant execute on function browse_stats(
  text[], text[], integer, integer, integer, integer,
  boolean, integer, boolean, boolean, boolean, boolean,
  text, boolean, boolean, boolean, integer
) to anon;


drop function if exists region_stats(
  text[], double precision, double precision, integer
);

create or replace function region_stats(
  districts_filter        text[]           default null,
  center_lng              double precision default null,
  center_lat              double precision default null,
  radius_m                integer          default null,
  category_sub_cb_filter  integer          default null
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
      and (category_sub_cb_filter is null or category_sub_cb = category_sub_cb_filter)
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
    'total_ever',             (select count(*)::int from filtered),
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
  text[], double precision, double precision, integer, integer
) to anon;
