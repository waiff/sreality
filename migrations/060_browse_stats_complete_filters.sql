-- 060_browse_stats_complete_filters.sql
--
-- Close the parity gap between the Browse page's PostgREST query and
-- the Stats tab's `browse_stats` RPC. Before this migration the RPC
-- silently ignored five filters that the Browse sidebar collects:
-- ownership, estate_area_min/max, usable_area_min/max, parking_lots
-- min, and the (newly-registered) garden_area_min/max. With those
-- filters set, Map / Table counts disagreed with Stats counts because
-- Stats applied a strict subset of the predicates.
--
-- This file adds those parameters to the function signature and the
-- `filtered` CTE. DROP-then-CREATE because the parameter list changes
-- (the anon grant is signature-pinned). Matches the pattern from 024
-- / 033 / 039 / 040 / 055.
--
-- The new params default to NULL so every existing caller (the
-- frontend's call_browse_stats helper, smoke tests, agent-side checks)
-- stays correct without touching their call sites — they simply don't
-- pass the new args. Once the unified filter registry's <FilterForm>
-- lands the SPA will pass them explicitly.

begin;

drop function if exists browse_stats(
  text[], text[], integer, integer, integer, integer,
  boolean, integer, integer, integer, integer, integer, integer,
  boolean, boolean, boolean, boolean,
  text, boolean, boolean, boolean, integer, text[], bigint[], text, text,
  double precision, double precision, double precision, double precision
);

create or replace function browse_stats(
  districts_filter         text[]   default null,
  dispositions_filter      text[]   default null,
  price_min_filter         integer  default null,
  price_max_filter         integer  default null,
  area_min_filter          integer  default null,
  area_max_filter          integer  default null,
  active_only_filter       boolean  default false,
  last_seen_min_days       integer  default null,
  last_seen_max_days       integer  default null,
  first_seen_min_days      integer  default null,
  first_seen_max_days      integer  default null,
  tom_days_min             integer  default null,
  tom_days_max             integer  default null,
  has_balcony_filter       boolean  default null,
  has_lift_filter          boolean  default null,
  has_parking_filter       boolean  default null,
  inactive_only_filter     boolean  default false,
  furnished_filter         text     default null,
  terrace_filter           boolean  default null,
  cellar_filter            boolean  default null,
  garage_filter            boolean  default null,
  category_sub_cb_filter   integer  default null,
  building_type_filter     text[]   default null,
  tag_ids                  bigint[] default null,
  category_main_filter     text     default null,
  category_type_filter     text     default null,
  bbox_west                double precision default null,
  bbox_south               double precision default null,
  bbox_east                double precision default null,
  bbox_north               double precision default null,
  -- New in 060: bring Stats in line with Map / Table.
  ownership_filter         text     default null,
  estate_area_min_filter   double precision default null,
  estate_area_max_filter   double precision default null,
  usable_area_min_filter   double precision default null,
  usable_area_max_filter   double precision default null,
  parking_lots_min_filter  integer  default null,
  garden_area_min_filter   double precision default null,
  garden_area_max_filter   double precision default null
)
returns jsonb
language sql
stable
security invoker
as $$
  with filtered as (
    select
      sreality_id, first_seen_at, last_seen_at, is_active,
      price_czk, area_m2, disposition, tom_days
    from listings_public
    where
          (not active_only_filter   or is_active = true)
      and (not inactive_only_filter or is_active = false)
      and (last_seen_max_days is null
           or last_seen_at >= now() - (last_seen_max_days || ' days')::interval)
      and (last_seen_min_days is null
           or last_seen_at <= now() - (last_seen_min_days || ' days')::interval)
      and (first_seen_max_days is null
           or first_seen_at >= now() - (first_seen_max_days || ' days')::interval)
      and (first_seen_min_days is null
           or first_seen_at <= now() - (first_seen_min_days || ' days')::interval)
      and (tom_days_min is null or tom_days >= tom_days_min)
      and (tom_days_max is null or tom_days <= tom_days_max)
      and (category_main_filter   is null or category_main   = category_main_filter)
      and (category_type_filter   is null or category_type   = category_type_filter)
      and (districts_filter       is null or district        = any(districts_filter))
      and (dispositions_filter    is null or disposition     = any(dispositions_filter))
      and (price_min_filter       is null or price_czk      >= price_min_filter)
      and (price_max_filter       is null or price_czk      <= price_max_filter)
      and (area_min_filter        is null or area_m2        >= area_min_filter)
      and (area_max_filter        is null or area_m2        <= area_max_filter)
      and (has_balcony_filter     is null or has_balcony     = has_balcony_filter)
      and (has_lift_filter        is null or has_lift        = has_lift_filter)
      and (has_parking_filter     is null or has_parking     = has_parking_filter)
      and (furnished_filter       is null or furnished       = furnished_filter)
      and (terrace_filter         is null or terrace         = terrace_filter)
      and (cellar_filter          is null or cellar          = cellar_filter)
      and (garage_filter          is null or garage          = garage_filter)
      and (category_sub_cb_filter is null or category_sub_cb = category_sub_cb_filter)
      and (building_type_filter   is null or array_length(building_type_filter, 1) is null
           or building_type = any(building_type_filter))
      -- New in 060.
      and (ownership_filter        is null or ownership      = ownership_filter)
      and (estate_area_min_filter  is null or estate_area   >= estate_area_min_filter)
      and (estate_area_max_filter  is null or estate_area   <= estate_area_max_filter)
      and (usable_area_min_filter  is null or usable_area   >= usable_area_min_filter)
      and (usable_area_max_filter  is null or usable_area   <= usable_area_max_filter)
      and (parking_lots_min_filter is null or parking_lots  >= parking_lots_min_filter)
      and (garden_area_min_filter  is null or garden_area   >= garden_area_min_filter)
      and (garden_area_max_filter  is null or garden_area   <= garden_area_max_filter)
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
      count(*)::int as n,
      count(price_czk::numeric / nullif(area_m2, 0))::int as ppm2_n,
      min(price_czk::numeric / nullif(area_m2, 0))::int as ppm2_min,
      percentile_cont(0.25) within group (
        order by price_czk::numeric / nullif(area_m2, 0)
      )::int as ppm2_p25,
      percentile_cont(0.50) within group (
        order by price_czk::numeric / nullif(area_m2, 0)
      )::int as ppm2_median,
      percentile_cont(0.75) within group (
        order by price_czk::numeric / nullif(area_m2, 0)
      )::int as ppm2_p75,
      max(price_czk::numeric / nullif(area_m2, 0))::int as ppm2_max
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
                      (select jsonb_agg(jsonb_build_object(
                          'disposition', disposition,
                          'n',           n,
                          'ppm2_box',    case when ppm2_n > 0
                                              then jsonb_build_object(
                                                'n',      ppm2_n,
                                                'min',    ppm2_min,
                                                'p25',    ppm2_p25,
                                                'median', ppm2_median,
                                                'p75',    ppm2_p75,
                                                'max',    ppm2_max
                                              )
                                              else null end
                        ))
                       from disposition_dist),
                      '[]'::jsonb
                    )
  );
$$;

grant execute on function browse_stats(
  text[], text[], integer, integer, integer, integer,
  boolean, integer, integer, integer, integer, integer, integer,
  boolean, boolean, boolean, boolean,
  text, boolean, boolean, boolean, integer, text[], bigint[], text, text,
  double precision, double precision, double precision, double precision,
  text,
  double precision, double precision,
  double precision, double precision,
  integer,
  double precision, double precision
) to anon;

commit;
