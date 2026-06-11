-- 173_price_change_filters.sql
--
-- Browse/Watchdog filter restructure (price-history merge + estimates flag):
--
-- 1. `properties` gains five derived price-history columns, maintained by
--    scripts/recompute_property_stats.py (same job that owns price_drop_count):
--      price_change_count       -- cuts + raises, all time
--      price_change_count_30d   -- cuts + raises inside the last 30 days
--      price_change_count_90d   --   ... 90 days
--      price_change_count_365d  --   ... 365 days
--      total_price_change_pct   -- signed (last - first) / first * 100 across
--                                  the property's combined price series; NULL
--                                  when fewer than two price points exist.
--    These back the merged `price_change_count_min` (+ window) and
--    `total_price_change_pct` filters that replace the retired per-direction
--    quartet (distinct_site_count_min / price_drop_count_min /
--    price_rise_count_min / max_price_drop_pct_min). The OLD columns stay --
--    the recompute job still maintains them and properties_public still
--    exposes them for display; only the filters are gone.
--    Backfill: dispatch the "recompute property stats" workflow (full mode)
--    after deploy; until it completes the new columns sit at 0 / NULL.
--
-- 2. `property_estimates_public`: anon-readable property-grain "we ran an
--    estimate here" view (estimation_runs is RLS-gated; this is the standard
--    *_public projection). Backs the Browse-only `with_estimates` filter --
--    Map/Table/Cards prefilter via `.in('property_id', ids)`, the Stats RPC
--    via EXISTS.
--
-- 3. `properties_public` appends the five new columns (CREATE OR REPLACE is
--    legal: columns are appended at the end, nothing else moves).
--
-- 4. `browse_stats_properties` re-signature: the four retired params go away,
--    and it gains building/apartment condition level min+max (closing the
--    long-standing gap where condition levels narrowed Map/Table/Cards but
--    never Stats), the merged price-change trio, and `with_estimates`.
--    `total_price_change_pct_filter` carries the `_filter` suffix because the
--    bare name would collide with the new properties_public column inside
--    plpgsql. Body reproduced VERBATIM from migration 172 except the
--    documented clause edits.

alter table properties
  add column price_change_count      integer not null default 0,
  add column price_change_count_30d  integer not null default 0,
  add column price_change_count_90d  integer not null default 0,
  add column price_change_count_365d integer not null default 0,
  add column total_price_change_pct  numeric;


-- 2. property-grain estimates projection ---------------------------------

create view property_estimates_public as
select
  l.property_id,
  count(*)::int      as run_count,
  max(er.created_at) as last_run_at
from estimation_runs er
join listings l on l.sreality_id = er.input_sreality_id
where er.status = 'success'
  and l.property_id is not null
group by l.property_id;

grant select on property_estimates_public to anon;


-- 3. properties_public + new columns --------------------------------------
-- Reproduced VERBATIM from migration 171; the ONLY change is the five
-- trailing price-history columns.

create or replace view properties_public as
 SELECT p.id AS property_id, p.repr_listing_id AS sreality_id, p.first_seen_at,
    p.last_seen_at, p.is_active, p.category_main, p.category_type,
    p.current_price_czk AS price_czk, l.price_unit, p.area_m2, p.disposition,
    p.locality, p.district, l.locality_district_id, l.locality_region_id,
    st_y(p.geom::geometry) AS lat, st_x(p.geom::geometry) AS lng,
    l.floor, l.total_floors, p.has_balcony, p.has_parking, p.has_lift,
    p.building_type, p.condition, l.energy_rating, p.estate_area, p.usable_area,
    p.garden_area, p.category_sub_cb, p.furnished, p.terrace, p.cellar, p.garage,
    p.parking_lots, p.ownership, l.broker_name, l.broker_email, l.broker_phone,
        CASE WHEN p.is_active THEN GREATEST(0, floor(EXTRACT(epoch FROM now() - p.first_seen_at) / 86400::numeric)::integer)
             ELSE GREATEST(0, floor(EXTRACT(epoch FROM p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer) END AS tom_days,
        CASE WHEN p.area_m2 IS NOT NULL AND p.area_m2 > 0::numeric AND p.current_price_czk IS NOT NULL THEN p.current_price_czk::numeric / p.area_m2
             ELSE NULL::numeric END AS price_per_m2,
    l.building_condition_level, l.apartment_condition_level, l.description,
    p.source_count, p.distinct_site_count, p.price_drop_count, p.price_rise_count,
    p.max_price_drop_pct, p.stats_computed_at, l.source, l.street,
    l.mf_reference_rent_czk, l.mf_gross_yield_pct,
    l.obec, l.okres, l.region,
    p.home_obec_pop, p.near_pop_5km, p.near_pop_15km, p.near_jobs_5km,
    p.near_jobs_15km, p.near_youth_5km, p.near_youth_15km, p.near_overall_5km,
    p.near_overall_15km,
    p.subtype,
    p.last_change_at,
    l.obec_id,
    l.okres_id, l.region_id,
    p.price_change_count, p.price_change_count_30d, p.price_change_count_90d,
    p.price_change_count_365d, p.total_price_change_pct
   FROM properties p
     LEFT JOIN listings l ON l.sreality_id = p.repr_listing_id
  WHERE p.status = 'active'::text;

grant select on properties_public to anon;


-- 4. browse_stats_properties re-signature ----------------------------------
-- Exact-signature DROP of the migration-172 overload (68 params).

drop function if exists public.browse_stats_properties(
  text[], text[], integer, integer, integer, integer, boolean, integer, integer, integer, integer, integer, integer, boolean, boolean, boolean, boolean, text[], boolean, boolean, boolean, integer, text[], bigint[], text, text, double precision, double precision, double precision, double precision, text[], double precision, double precision, double precision, double precision, integer, double precision, double precision, text[], text[], jsonb, integer, integer, jsonb, double precision, double precision, integer, integer, integer, double precision, text[], double precision, double precision, integer, integer, double precision, double precision, double precision, double precision, double precision, double precision, boolean[], text[], integer, integer, bigint[], text[], bigint[]
);

CREATE OR REPLACE FUNCTION public.browse_stats_properties(districts_filter text[] DEFAULT NULL::text[], dispositions_filter text[] DEFAULT NULL::text[], price_min_filter integer DEFAULT NULL::integer, price_max_filter integer DEFAULT NULL::integer, area_min_filter integer DEFAULT NULL::integer, area_max_filter integer DEFAULT NULL::integer, active_only_filter boolean DEFAULT false, last_seen_min_days integer DEFAULT NULL::integer, last_seen_max_days integer DEFAULT NULL::integer, first_seen_min_days integer DEFAULT NULL::integer, first_seen_max_days integer DEFAULT NULL::integer, tom_days_min integer DEFAULT NULL::integer, tom_days_max integer DEFAULT NULL::integer, has_balcony_filter boolean DEFAULT NULL::boolean, has_lift_filter boolean DEFAULT NULL::boolean, has_parking_filter boolean DEFAULT NULL::boolean, inactive_only_filter boolean DEFAULT false, furnished_filter text[] DEFAULT NULL::text[], terrace_filter boolean DEFAULT NULL::boolean, cellar_filter boolean DEFAULT NULL::boolean, garage_filter boolean DEFAULT NULL::boolean, category_sub_cb_filter integer DEFAULT NULL::integer, building_type_filter text[] DEFAULT NULL::text[], tag_ids bigint[] DEFAULT NULL::bigint[], category_main_filter text DEFAULT NULL::text, category_type_filter text DEFAULT NULL::text, bbox_west double precision DEFAULT NULL::double precision, bbox_south double precision DEFAULT NULL::double precision, bbox_east double precision DEFAULT NULL::double precision, bbox_north double precision DEFAULT NULL::double precision, ownership_filter text[] DEFAULT NULL::text[], estate_area_min_filter double precision DEFAULT NULL::double precision, estate_area_max_filter double precision DEFAULT NULL::double precision, usable_area_min_filter double precision DEFAULT NULL::double precision, usable_area_max_filter double precision DEFAULT NULL::double precision, parking_lots_min_filter integer DEFAULT NULL::integer, garden_area_min_filter double precision DEFAULT NULL::double precision, garden_area_max_filter double precision DEFAULT NULL::double precision, condition_match_filter text[] DEFAULT NULL::text[], districts_context_filter text[] DEFAULT NULL::text[], city_index_rules jsonb DEFAULT NULL::jsonb, city_pop_min integer DEFAULT NULL::integer, city_pop_max integer DEFAULT NULL::integer, city_proximity jsonb DEFAULT NULL::jsonb, price_per_m2_min double precision DEFAULT NULL::double precision, price_per_m2_max double precision DEFAULT NULL::double precision, portal_filter text[] DEFAULT NULL::text[], mf_gross_yield_pct_min double precision DEFAULT NULL::double precision, mf_gross_yield_pct_max double precision DEFAULT NULL::double precision, near_pop_5km_min integer DEFAULT NULL::integer, near_pop_15km_min integer DEFAULT NULL::integer, near_jobs_5km_min double precision DEFAULT NULL::double precision, near_jobs_15km_min double precision DEFAULT NULL::double precision, near_youth_5km_min double precision DEFAULT NULL::double precision, near_youth_15km_min double precision DEFAULT NULL::double precision, near_overall_5km_min double precision DEFAULT NULL::double precision, near_overall_15km_min double precision DEFAULT NULL::double precision, districts_excluded_filter boolean[] DEFAULT NULL::boolean[], subtype_filter text[] DEFAULT NULL::text[], recently_added_days integer DEFAULT NULL::integer, recently_changed_days integer DEFAULT NULL::integer, obec_ids_filter bigint[] DEFAULT NULL::bigint[], districts_levels text[] DEFAULT NULL::text[], districts_ids bigint[] DEFAULT NULL::bigint[], building_condition_level_min integer DEFAULT NULL::integer, building_condition_level_max integer DEFAULT NULL::integer, apartment_condition_level_min integer DEFAULT NULL::integer, apartment_condition_level_max integer DEFAULT NULL::integer, price_change_count_min integer DEFAULT NULL::integer, price_change_window_days integer DEFAULT NULL::integer, total_price_change_pct_filter double precision DEFAULT NULL::double precision, with_estimates boolean DEFAULT false)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
begin
  return (
  with filtered as (
    select l.sreality_id, l.first_seen_at, l.last_seen_at, l.is_active, l.price_czk, l.area_m2, l.disposition, l.tom_days
    from properties_public l
    where
          (not active_only_filter   or l.is_active = true)
      and (not inactive_only_filter or l.is_active = false)
      and (last_seen_max_days is null or l.last_seen_at >= now() - (last_seen_max_days || ' days')::interval)
      and (last_seen_min_days is null or l.last_seen_at <= now() - (last_seen_min_days || ' days')::interval)
      and (first_seen_max_days is null or l.first_seen_at >= now() - (first_seen_max_days || ' days')::interval)
      and (first_seen_min_days is null or l.first_seen_at <= now() - (first_seen_min_days || ' days')::interval)
      and (recently_added_days   is null or l.first_seen_at  >= now() - (recently_added_days   || ' days')::interval)
      and (recently_changed_days is null or l.last_change_at >= now() - (recently_changed_days || ' days')::interval)
      and (tom_days_min is null or l.tom_days >= tom_days_min)
      and (tom_days_max is null or l.tom_days <= tom_days_max)
      and (category_main_filter   is null or l.category_main   = category_main_filter)
      and (category_type_filter   is null or l.category_type   = category_type_filter)
      and (
        districts_filter is null or array_length(districts_filter, 1) is null
        or not exists (
          select 1 from unnest(districts_filter,
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, excl, ord)
          where not coalesce(excl, false)
        )
        or exists (
          select 1 from unnest(districts_filter,
                 coalesce(districts_context_filter, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)])),
                 coalesce(districts_levels, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_ids, array_fill(null::bigint, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, ctx, excl, lvl, admin_id, ord)
          where not coalesce(excl, false)
            and case
              when lvl = 'obec'  and admin_id is not null then l.obec_id   = admin_id
              when lvl = 'okres' and admin_id is not null then l.okres_id  = admin_id
              when lvl = 'kraj'  and admin_id is not null then l.region_id = admin_id
              when lvl = 'locality' then (admin_id is null or l.obec_id = admin_id) and l.locality ilike '%' || needle || '%'
              else (l.district ilike '%' || needle || '%' or l.locality ilike '%' || needle || '%'
                    or l.okres ilike '%' || needle || '%' or l.region ilike '%' || needle || '%')
                and (ctx is null or ctx = '' or l.district ilike '%' || ctx || '%' or l.locality ilike '%' || ctx || '%'
                     or l.okres ilike '%' || ctx || '%' or l.region ilike '%' || ctx || '%')
            end
        )
      )
      and (
        districts_filter is null or array_length(districts_filter, 1) is null
        or not exists (
          select 1 from unnest(districts_filter,
                 coalesce(districts_context_filter, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)])),
                 coalesce(districts_levels, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_ids, array_fill(null::bigint, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, ctx, excl, lvl, admin_id, ord)
          where coalesce(excl, false)
            and case
              when lvl = 'obec'  and admin_id is not null then l.obec_id   = admin_id
              when lvl = 'okres' and admin_id is not null then l.okres_id  = admin_id
              when lvl = 'kraj'  and admin_id is not null then l.region_id = admin_id
              when lvl = 'locality' then (admin_id is null or l.obec_id = admin_id) and l.locality ilike '%' || needle || '%'
              else (l.district ilike '%' || needle || '%' or l.locality ilike '%' || needle || '%'
                    or l.okres ilike '%' || needle || '%' or l.region ilike '%' || needle || '%')
                and (ctx is null or ctx = '' or l.district ilike '%' || ctx || '%' or l.locality ilike '%' || ctx || '%'
                     or l.okres ilike '%' || ctx || '%' or l.region ilike '%' || ctx || '%')
            end
        )
      )
      and (dispositions_filter    is null or l.disposition     = any(dispositions_filter))
      and (price_min_filter       is null or l.price_czk      >= price_min_filter)
      and (price_max_filter       is null or l.price_czk      <= price_max_filter)
      and (area_min_filter        is null or l.area_m2        >= area_min_filter)
      and (area_max_filter        is null or l.area_m2        <= area_max_filter)
      and (price_per_m2_min is null or (l.area_m2 is not null and l.area_m2 > 0 and l.price_czk::numeric / l.area_m2 >= price_per_m2_min))
      and (price_per_m2_max is null or (l.area_m2 is not null and l.area_m2 > 0 and l.price_czk::numeric / l.area_m2 <= price_per_m2_max))
      and (mf_gross_yield_pct_min is null or l.mf_gross_yield_pct >= mf_gross_yield_pct_min)
      and (mf_gross_yield_pct_max is null or l.mf_gross_yield_pct <= mf_gross_yield_pct_max)
      and (has_balcony_filter     is null or l.has_balcony     = has_balcony_filter)
      and (has_lift_filter        is null or l.has_lift        = has_lift_filter)
      and (has_parking_filter     is null or l.has_parking     = has_parking_filter)
      and (
        furnished_filter is null or array_length(furnished_filter, 1) is null
        or l.furnished = any(furnished_filter)
        or ('__unknown__' = any(furnished_filter)
            and (l.furnished is null or not (l.furnished = any(array['ano','ne','castecne']))))
      )
      and (terrace_filter         is null or l.terrace         = terrace_filter)
      and (cellar_filter          is null or l.cellar          = cellar_filter)
      and (garage_filter          is null or l.garage          = garage_filter)
      and (category_sub_cb_filter is null or l.category_sub_cb = category_sub_cb_filter)
      and (subtype_filter is null or array_length(subtype_filter, 1) is null or l.subtype = any(subtype_filter))
      and (building_type_filter   is null or array_length(building_type_filter, 1) is null or l.building_type = any(building_type_filter))
      and (condition_match_filter is null or array_length(condition_match_filter, 1) is null or l.condition = any(condition_match_filter))
      and (portal_filter is null or array_length(portal_filter, 1) is null or l.source = any(portal_filter))
      and (
        ownership_filter is null or array_length(ownership_filter, 1) is null
        or l.ownership = any(ownership_filter)
        or ('__unknown__' = any(ownership_filter)
            and (l.ownership is null or not (l.ownership = any(array['osobni','druzstevni','statni']))))
      )
      and (estate_area_min_filter  is null or l.estate_area   >= estate_area_min_filter)
      and (estate_area_max_filter  is null or l.estate_area   <= estate_area_max_filter)
      and (usable_area_min_filter  is null or l.usable_area   >= usable_area_min_filter)
      and (usable_area_max_filter  is null or l.usable_area   <= usable_area_max_filter)
      and (parking_lots_min_filter is null or l.parking_lots  >= parking_lots_min_filter)
      and (garden_area_min_filter  is null or l.garden_area   >= garden_area_min_filter)
      and (garden_area_max_filter  is null or l.garden_area   <= garden_area_max_filter)
      and (bbox_west  is null or l.lng >= bbox_west)
      and (bbox_east  is null or l.lng <= bbox_east)
      and (bbox_south is null or l.lat >= bbox_south)
      and (bbox_north is null or l.lat <= bbox_north)
      and (building_condition_level_min  is null or l.building_condition_level  >= building_condition_level_min)
      and (building_condition_level_max  is null or l.building_condition_level  <= building_condition_level_max)
      and (apartment_condition_level_min is null or l.apartment_condition_level >= apartment_condition_level_min)
      and (apartment_condition_level_max is null or l.apartment_condition_level <= apartment_condition_level_max)
      and (price_change_count_min is null or
           (case when price_change_window_days = 30  then l.price_change_count_30d
                 when price_change_window_days = 90  then l.price_change_count_90d
                 when price_change_window_days = 365 then l.price_change_count_365d
                 else l.price_change_count end) >= price_change_count_min)
      and (total_price_change_pct_filter is null or total_price_change_pct_filter = 0
           or (total_price_change_pct_filter < 0 and l.total_price_change_pct <= total_price_change_pct_filter)
           or (total_price_change_pct_filter > 0 and l.total_price_change_pct >= total_price_change_pct_filter))
      and (not coalesce(with_estimates, false) or exists (
            select 1 from property_estimates_public pe where pe.property_id = l.property_id))
      and (obec_ids_filter is null or l.obec_id = any(obec_ids_filter))
      and (tag_ids is null or array_length(tag_ids, 1) is null or l.sreality_id in (
          select lt.sreality_id from listing_tags lt where lt.tag_id = any(tag_ids)
          group by lt.sreality_id having count(distinct lt.tag_id) = array_length(tag_ids, 1)))
      and (city_pop_min is null or l.home_obec_pop >= city_pop_min)
      and (city_pop_max is null or l.home_obec_pop <= city_pop_max)
      and (near_pop_5km_min      is null or l.near_pop_5km      >= near_pop_5km_min)
      and (near_pop_15km_min     is null or l.near_pop_15km     >= near_pop_15km_min)
      and (near_jobs_5km_min     is null or l.near_jobs_5km     >= near_jobs_5km_min)
      and (near_jobs_15km_min    is null or l.near_jobs_15km    >= near_jobs_15km_min)
      and (near_youth_5km_min    is null or l.near_youth_5km    >= near_youth_5km_min)
      and (near_youth_15km_min   is null or l.near_youth_15km   >= near_youth_15km_min)
      and (near_overall_5km_min  is null or l.near_overall_5km  >= near_overall_5km_min)
      and (near_overall_15km_min is null or l.near_overall_15km >= near_overall_15km_min)
      and ((city_index_rules is null or jsonb_array_length(city_index_rules) = 0)
        or (l.lat is not null and l.lng is not null and exists (
            select 1 from curated_cities_public c
            where st_dwithin(st_setsrid(st_makepoint(l.lng, l.lat), 4326)::geography, st_setsrid(st_makepoint(c.lng, c.lat), 4326)::geography, c.default_radius_m)
              and not exists (select 1 from jsonb_array_elements(coalesce(city_index_rules, '[]'::jsonb)) r
                where not exists (select 1 from city_index_values_public v where v.city_id = c.city_id and v.index_name = r->>'index_name' and v.value >= (r->>'value')::numeric)))))
      and (city_proximity is null or (l.lat is not null and l.lng is not null and exists (
            select 1 from curated_cities_public c
            where st_dwithin(st_setsrid(st_makepoint(l.lng, l.lat), 4326)::geography, st_setsrid(st_makepoint(c.lng, c.lat), 4326)::geography, ((city_proximity ->> 'radius_km')::int * 1000))
              and ((city_proximity ->> 'population_min')::int is null or c.population >= (city_proximity ->> 'population_min')::int)
              and not exists (select 1 from jsonb_array_elements(coalesce(city_proximity -> 'index_rules', '[]'::jsonb)) r
                where not exists (select 1 from city_index_values_public v where v.city_id = c.city_id and v.index_name = r->>'index_name' and v.value >= (r->>'value')::numeric)))))
  ),
  price_pct as (select percentile_cont(0.25) within group (order by price_czk)::int as p25, percentile_cont(0.50) within group (order by price_czk)::int as p50, percentile_cont(0.75) within group (order by price_czk)::int as p75 from filtered where price_czk is not null),
  ppm2_pct as (select percentile_cont(0.25) within group (order by price_czk::numeric / area_m2)::int as p25, percentile_cont(0.50) within group (order by price_czk::numeric / area_m2)::int as p50, percentile_cont(0.75) within group (order by price_czk::numeric / area_m2)::int as p75 from filtered where price_czk is not null and area_m2 is not null and area_m2 > 0),
  disposition_dist as (select coalesce(disposition, 'unspecified') as disposition, count(*)::int as n, count(price_czk::numeric / nullif(area_m2, 0))::int as ppm2_n, min(price_czk::numeric / nullif(area_m2, 0))::int as ppm2_min, percentile_cont(0.25) within group (order by price_czk::numeric / nullif(area_m2, 0))::int as ppm2_p25, percentile_cont(0.50) within group (order by price_czk::numeric / nullif(area_m2, 0))::int as ppm2_median, percentile_cont(0.75) within group (order by price_czk::numeric / nullif(area_m2, 0))::int as ppm2_p75, max(price_czk::numeric / nullif(area_m2, 0))::int as ppm2_max from filtered group by disposition order by n desc, disposition asc),
  price_cuts as (select percentile_cont(0.10) within group (order by price_czk) as cut_10, percentile_cont(0.25) within group (order by price_czk) as cut_25, percentile_cont(0.45) within group (order by price_czk) as cut_45, percentile_cont(0.55) within group (order by price_czk) as cut_55, percentile_cont(0.75) within group (order by price_czk) as cut_75, percentile_cont(0.90) within group (order by price_czk) as cut_90, count(*)::int as priced_total from filtered where price_czk is not null),
  price_bands as (select f.price_czk, f.tom_days, case when f.price_czk <= c.cut_10 then 1 when f.price_czk <= c.cut_25 then 2 when f.price_czk <= c.cut_45 then 3 when f.price_czk <= c.cut_55 then 4 when f.price_czk <= c.cut_75 then 5 when f.price_czk <= c.cut_90 then 6 else 7 end as bucket, c.priced_total from filtered f, price_cuts c where f.price_czk is not null),
  band_definitions(bucket, p_lo, p_hi) as (values (1, 0, 10), (2, 10, 25), (3, 25, 45), (4, 45, 55), (5, 55, 75), (6, 75, 90), (7, 90, 100)),
  band_stats as (select d.bucket, d.p_lo, d.p_hi, count(b.price_czk)::int as n, max(b.priced_total) as priced_total, min(b.price_czk)::int as price_min, max(b.price_czk)::int as price_max, count(b.tom_days)::int as tom_n, min(b.tom_days)::int as tom_min, percentile_cont(0.25) within group (order by b.tom_days) filter (where b.tom_days is not null) as tom_p25, percentile_cont(0.50) within group (order by b.tom_days) filter (where b.tom_days is not null) as tom_median, percentile_cont(0.75) within group (order by b.tom_days) filter (where b.tom_days is not null) as tom_p75, max(b.tom_days)::int as tom_max, avg(b.tom_days) filter (where b.tom_days is not null) as tom_mean from band_definitions d left join price_bands b on b.bucket = d.bucket group by d.bucket, d.p_lo, d.p_hi order by d.bucket)
  select jsonb_build_object(
    'total', (select count(*)::int from filtered),
    'new_7d', (select count(*)::int from filtered where first_seen_at >= now() - interval '7 days'),
    'new_30d', (select count(*)::int from filtered where first_seen_at >= now() - interval '30 days'),
    'price', (select case when p50 is null then null else jsonb_build_object('p25', p25, 'p50', p50, 'p75', p75) end from price_pct),
    'ppm2', (select case when p50 is null then null else jsonb_build_object('p25', p25, 'p50', p50, 'p75', p75) end from ppm2_pct),
    'dispositions', coalesce((select jsonb_agg(jsonb_build_object('disposition', disposition, 'n', n, 'ppm2_box', case when ppm2_n > 0 then jsonb_build_object('n', ppm2_n, 'min', ppm2_min, 'p25', ppm2_p25, 'median', ppm2_median, 'p75', ppm2_p75, 'max', ppm2_max) else null end)) from disposition_dist), '[]'::jsonb),
    'price_band_velocity', coalesce((select jsonb_agg(jsonb_build_object('bucket', bs.bucket, 'p_lo', bs.p_lo, 'p_hi', bs.p_hi, 'n', bs.n, 'pct_share', case when bs.priced_total is null or bs.priced_total = 0 then null else round(bs.n * 100.0 / bs.priced_total, 1) end, 'price_min', bs.price_min, 'price_max', bs.price_max, 'tom_box', case when bs.tom_n > 0 then jsonb_build_object('n', bs.tom_n, 'min', bs.tom_min, 'p25', round(bs.tom_p25::numeric, 1), 'median', round(bs.tom_median::numeric, 1), 'mean', round(bs.tom_mean::numeric, 1), 'p75', round(bs.tom_p75::numeric, 1), 'max', bs.tom_max) else null end) order by bs.p_lo) from band_stats bs), '[]'::jsonb)
  )
  );
end
$function$;
