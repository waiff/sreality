-- 040_browse_stats_bbox.sql
-- Extend browse_stats() with bbox arguments so the Stats tab respects
-- the map viewport. The Browse page's middle column, table, and stats
-- all narrow to whatever the operator has the map zoomed to; the
-- map area lives in the URL as ?bbox=west,south,east,north and the
-- frontend round-trips it through this RPC for the Stats numbers.
--
-- Bbox values gate the filtered CTE with simple lat/lng range
-- predicates against the geom point stored on each listing. NULL or
-- omitted values skip the predicate entirely — backward compatible
-- with every existing caller.
--
-- DROP-then-CREATE matches the pattern set by 024 / 033 / 039.
-- Postgres identifies functions by full argument signature, so
-- adding parameters would otherwise leave a stale overload behind
-- and the SPA's by-name calls would become ambiguous.

drop function if exists browse_stats(
  text[], text[], integer, integer, integer, integer,
  boolean, integer, boolean, boolean, boolean, boolean,
  text, boolean, boolean, boolean, integer, bigint[], text, text
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
  category_sub_cb_filter   integer  default null,
  tag_ids                  bigint[] default null,
  category_main_filter     text     default null,
  category_type_filter     text     default null,
  bbox_west                double precision default null,
  bbox_south               double precision default null,
  bbox_east                double precision default null,
  bbox_north               double precision default null
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
      and (category_main_filter   is null or category_main   = category_main_filter)
      and (category_type_filter   is null or category_type   = category_type_filter)
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
      and (bbox_west  is null or lng >= bbox_west)
      and (bbox_east  is null or lng <= bbox_east)
      and (bbox_south is null or lat >= bbox_south)
      and (bbox_north is null or lat <= bbox_north)
      and (
        tag_ids is null
        or array_length(tag_ids, 1) is null
        or sreality_id in (
          select lt.sreality_id
          from listing_tags lt
          where lt.tag_id = any(tag_ids)
          group by lt.sreality_id
          having count(distinct lt.tag_id) = array_length(tag_ids, 1)
        )
      )
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
  text, boolean, boolean, boolean, integer, bigint[], text, text,
  double precision, double precision, double precision, double precision
) to anon;
