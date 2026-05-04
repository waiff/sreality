-- 011_browse_stats.sql
--
-- Aggregate stats for the Browse page's Stats tab. One RPC, one filter
-- scan, returns count + percentiles + disposition distribution + new-N-day
-- counts as a single jsonb. PostgREST cannot express percentile_cont
-- directly (it's an ordered-set aggregate, not a regular aggregate), so
-- this function exists.
--
-- Read-only; reads listings_public so the column exposure boundary stays
-- where migration 008 set it. SECURITY INVOKER (no privilege escalation):
-- anon already has SELECT on listings_public.
--
-- Filter signature mirrors the URL-state shape used by frontend/src/lib/filters.ts.
-- Tri-state filters (balcony / lift / parking) use NULL for "any",
-- TRUE for "yes", FALSE for "no". seen_within_days = NULL means "any
-- last_seen_at", otherwise the cutoff is now() − that many days.

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
  has_parking_filter       boolean  default null
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
          (not active_only_filter or is_active = true)
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
    'price',        (select case when count(*) > 0 then jsonb_build_object('p25', p25, 'p50', p50, 'p75', p75) else null end from price_pct),
    'ppm2',         (select case when count(*) > 0 then jsonb_build_object('p25', p25, 'p50', p50, 'p75', p75) else null end from ppm2_pct),
    'dispositions', coalesce(
                      (select jsonb_agg(jsonb_build_object('disposition', disposition, 'n', n)) from disposition_dist),
                      '[]'::jsonb
                    )
  );
$$;

grant execute on function browse_stats(
  text[], text[], integer, integer, integer, integer,
  boolean, integer, boolean, boolean, boolean
) to anon;
