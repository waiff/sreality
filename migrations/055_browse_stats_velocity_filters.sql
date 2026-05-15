-- 055_browse_stats_velocity_filters.sql
-- Replace browse_stats() signature to support:
--   * TOM ("turned in") range filter via `tom_days_min` / `tom_days_max`
--     (the column added in migration 054)
--   * last_seen_at as a from-to days range via `last_seen_min_days` /
--     `last_seen_max_days` (replacing the old 1d/7d/30d preset
--     `seen_within_days_filter`)
--   * first_seen_at as a from-to days range via `first_seen_min_days` /
--     `first_seen_max_days`
--   * building material via `building_type_filter text[]` so the
--     "Cihla / Panel / Smíšená / Ostatní" UI bucket can either pass a
--     single value or the multi-value Ostatní expansion
--
-- Also flips the implicit freshness gate off: `active_only_filter`
-- defaults to false (no more "active by default"), and the old
-- `seen_within_days_filter` is removed. Callers that want the legacy
-- behaviour pass it explicitly. Operator confirmed this trade-off in
-- the plan (Q1 = "Flip everywhere") — Browse no longer hides delisted
-- or stale listings unless asked.
--
-- DROP-then-CREATE because the parameter list changes; anon's grant is
-- signature-pinned. Repeating the pattern from 024 / 033 / 039 / 040.

drop function if exists browse_stats(
  text[], text[], integer, integer, integer, integer,
  boolean, integer, boolean, boolean, boolean, boolean,
  text, boolean, boolean, boolean, integer, bigint[], text, text,
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
      price_czk, area_m2, disposition, tom_days
    from listings_public
    where
          (not active_only_filter   or is_active = true)
      and (not inactive_only_filter or is_active = false)
      -- last_seen days-ago range: max_days = farthest in the past,
      -- min_days = most recent allowed (e.g. min=3, max=10 -> last_seen
      -- between now()-10d and now()-3d).
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
  double precision, double precision, double precision, double precision
) to anon;
