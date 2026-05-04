-- 013_browse_stats_inactive_only.sql
--
-- Add `inactive_only_filter` to browse_stats so the Browse Stats tab can
-- mirror the new tri-state status pill in the UI (active / inactive / any).
-- Migration 011 only had `active_only_filter`, so picking "Inactive" in
-- the sidebar previously fell through to the "Any" stats. With this
-- migration the Stats tab matches the table and map exactly.
--
-- Drop-then-create because PostgreSQL identifies functions by full
-- argument signature; adding a parameter creates a new overload.
-- The new signature keeps `active_only_filter` first to preserve
-- positional ordering for any caller that might rely on it (the
-- frontend uses named params, so no impact there).
--
-- The two flags are independent in SQL but the frontend treats them as
-- mutually exclusive (`status` is a single enum). If a caller sets both
-- to true the query returns zero rows, which is correct: no listing is
-- both active and inactive.

drop function if exists browse_stats(
  text[], text[], integer, integer, integer, integer,
  boolean, integer, boolean, boolean, boolean
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
  inactive_only_filter     boolean  default false
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
      and (districts_filter    is null or district    = any(districts_filter))
      and (dispositions_filter is null or disposition = any(dispositions_filter))
      and (price_min_filter    is null or price_czk  >= price_min_filter)
      and (price_max_filter    is null or price_czk  <= price_max_filter)
      and (area_min_filter     is null or area_m2    >= area_min_filter)
      and (area_max_filter     is null or area_m2    <= area_max_filter)
      and (has_balcony_filter  is null or has_balcony = has_balcony_filter)
      and (has_lift_filter     is null or has_lift    = has_lift_filter)
      and (has_parking_filter  is null or has_parking = has_parking_filter)
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
  boolean, integer, boolean, boolean, boolean, boolean
) to anon;
