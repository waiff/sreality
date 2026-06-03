-- 159_browse_stats_recency.sql
--
-- Add two preset-style recency params to browse_stats_properties so the Browse
-- Stats tab stays consistent with Map / Table when the new "recently added" /
-- "recently changed" Status-section filters are active:
--   * recently_added_days   -> first_seen_at  >= now() - N days  (existing column)
--   * recently_changed_days -> last_change_at  >= now() - N days  (migration 158)
--
-- Reproduced VERBATIM from migration 155 (verified against the live
-- pg_get_functiondef); the ONLY changes are (1) the two new trailing params and
-- (2) two WHERE clauses placed with the other first/last-seen date predicates.
-- Both are plain indexed-timestamp comparisons on the denormalized
-- properties_public columns — no jsonb/spatial scan, so the anon 3s
-- statement-timeout budget is unaffected.
--
-- The signature changes, so the exact current overload is DROPped first (a
-- CREATE OR REPLACE alone would leave a duplicate overload). The listing-grain
-- browse_stats() is intentionally NOT touched — it has no live caller.

drop function if exists public.browse_stats_properties(
  text[], text[], integer, integer, integer, integer, boolean, integer, integer,
  integer, integer, integer, integer, boolean, boolean, boolean, boolean, text,
  boolean, boolean, boolean, integer, text[], bigint[], text, text,
  double precision, double precision, double precision, double precision, text,
  double precision, double precision, double precision, double precision, integer,
  double precision, double precision, text[], text[], jsonb, integer, integer,
  jsonb, double precision, double precision, integer, integer, integer,
  double precision, text[], double precision, double precision, integer, integer,
  double precision, double precision, double precision, double precision,
  double precision, double precision, boolean[], text[]
);

CREATE OR REPLACE FUNCTION public.browse_stats_properties(districts_filter text[] DEFAULT NULL::text[], dispositions_filter text[] DEFAULT NULL::text[], price_min_filter integer DEFAULT NULL::integer, price_max_filter integer DEFAULT NULL::integer, area_min_filter integer DEFAULT NULL::integer, area_max_filter integer DEFAULT NULL::integer, active_only_filter boolean DEFAULT false, last_seen_min_days integer DEFAULT NULL::integer, last_seen_max_days integer DEFAULT NULL::integer, first_seen_min_days integer DEFAULT NULL::integer, first_seen_max_days integer DEFAULT NULL::integer, tom_days_min integer DEFAULT NULL::integer, tom_days_max integer DEFAULT NULL::integer, has_balcony_filter boolean DEFAULT NULL::boolean, has_lift_filter boolean DEFAULT NULL::boolean, has_parking_filter boolean DEFAULT NULL::boolean, inactive_only_filter boolean DEFAULT false, furnished_filter text DEFAULT NULL::text, terrace_filter boolean DEFAULT NULL::boolean, cellar_filter boolean DEFAULT NULL::boolean, garage_filter boolean DEFAULT NULL::boolean, category_sub_cb_filter integer DEFAULT NULL::integer, building_type_filter text[] DEFAULT NULL::text[], tag_ids bigint[] DEFAULT NULL::bigint[], category_main_filter text DEFAULT NULL::text, category_type_filter text DEFAULT NULL::text, bbox_west double precision DEFAULT NULL::double precision, bbox_south double precision DEFAULT NULL::double precision, bbox_east double precision DEFAULT NULL::double precision, bbox_north double precision DEFAULT NULL::double precision, ownership_filter text DEFAULT NULL::text, estate_area_min_filter double precision DEFAULT NULL::double precision, estate_area_max_filter double precision DEFAULT NULL::double precision, usable_area_min_filter double precision DEFAULT NULL::double precision, usable_area_max_filter double precision DEFAULT NULL::double precision, parking_lots_min_filter integer DEFAULT NULL::integer, garden_area_min_filter double precision DEFAULT NULL::double precision, garden_area_max_filter double precision DEFAULT NULL::double precision, condition_match_filter text[] DEFAULT NULL::text[], districts_context_filter text[] DEFAULT NULL::text[], city_index_rules jsonb DEFAULT NULL::jsonb, city_pop_min integer DEFAULT NULL::integer, city_pop_max integer DEFAULT NULL::integer, city_proximity jsonb DEFAULT NULL::jsonb, price_per_m2_min double precision DEFAULT NULL::double precision, price_per_m2_max double precision DEFAULT NULL::double precision, distinct_site_count_min integer DEFAULT NULL::integer, price_drop_count_min integer DEFAULT NULL::integer, price_rise_count_min integer DEFAULT NULL::integer, max_price_drop_pct_min double precision DEFAULT NULL::double precision, portal_filter text[] DEFAULT NULL::text[], mf_gross_yield_pct_min double precision DEFAULT NULL::double precision, mf_gross_yield_pct_max double precision DEFAULT NULL::double precision, near_pop_5km_min integer DEFAULT NULL::integer, near_pop_15km_min integer DEFAULT NULL::integer, near_jobs_5km_min double precision DEFAULT NULL::double precision, near_jobs_15km_min double precision DEFAULT NULL::double precision, near_youth_5km_min double precision DEFAULT NULL::double precision, near_youth_15km_min double precision DEFAULT NULL::double precision, near_overall_5km_min double precision DEFAULT NULL::double precision, near_overall_15km_min double precision DEFAULT NULL::double precision, districts_excluded_filter boolean[] DEFAULT NULL::boolean[], subtype_filter text[] DEFAULT NULL::text[], recently_added_days integer DEFAULT NULL::integer, recently_changed_days integer DEFAULT NULL::integer)
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
                 coalesce(districts_context_filter, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, ctx, excl, ord)
          where not coalesce(excl, false)
        )
        or exists (
          select 1 from unnest(districts_filter,
                 coalesce(districts_context_filter, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, ctx, excl, ord)
          where not coalesce(excl, false)
            and (l.district ilike '%' || needle || '%' or l.locality ilike '%' || needle || '%'
                 or l.okres ilike '%' || needle || '%' or l.region ilike '%' || needle || '%')
            and (ctx is null or ctx = '' or l.district ilike '%' || ctx || '%' or l.locality ilike '%' || ctx || '%'
                 or l.okres ilike '%' || ctx || '%' or l.region ilike '%' || ctx || '%')
        )
      )
      and (
        districts_filter is null or array_length(districts_filter, 1) is null
        or not exists (
          select 1 from unnest(districts_filter,
                 coalesce(districts_context_filter, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, ctx, excl, ord)
          where coalesce(excl, false)
            and (l.district ilike '%' || needle || '%' or l.locality ilike '%' || needle || '%'
                 or l.okres ilike '%' || needle || '%' or l.region ilike '%' || needle || '%')
            and (ctx is null or ctx = '' or l.district ilike '%' || ctx || '%' or l.locality ilike '%' || ctx || '%'
                 or l.okres ilike '%' || ctx || '%' or l.region ilike '%' || ctx || '%')
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
      and (furnished_filter       is null or l.furnished       = furnished_filter)
      and (terrace_filter         is null or l.terrace         = terrace_filter)
      and (cellar_filter          is null or l.cellar          = cellar_filter)
      and (garage_filter          is null or l.garage          = garage_filter)
      and (category_sub_cb_filter is null or l.category_sub_cb = category_sub_cb_filter)
      and (subtype_filter is null or array_length(subtype_filter, 1) is null or l.subtype = any(subtype_filter))
      and (building_type_filter   is null or array_length(building_type_filter, 1) is null or l.building_type = any(building_type_filter))
      and (condition_match_filter is null or array_length(condition_match_filter, 1) is null or l.condition = any(condition_match_filter))
      and (portal_filter is null or array_length(portal_filter, 1) is null or l.source = any(portal_filter))
      and (ownership_filter        is null or l.ownership      = ownership_filter)
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
      and (distinct_site_count_min is null or l.distinct_site_count >= distinct_site_count_min)
      and (price_drop_count_min    is null or l.price_drop_count    >= price_drop_count_min)
      and (price_rise_count_min    is null or l.price_rise_count    >= price_rise_count_min)
      and (max_price_drop_pct_min  is null or l.max_price_drop_pct  >= max_price_drop_pct_min)
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
