-- 504_location_w3_one_code_predicate.sql
--
-- Location simplification sprint, wave W3, step S3: ONE CODE PREDICATE.
-- Re-creates the two RPC bodies that carry a copy of the place predicate, and
-- appends the fourth chip level to the two views a place-filtering surface
-- reads. Additive: no column is dropped, no signature changes, no data moves.
--
-- WHAT CHANGES, IN ONE LINE: a location chip is a LEVEL plus a RUIAN CODE, and
-- every surface tests it the same way -- `<level>_id = any(codes)` with plain
-- equality -- instead of five different predicates in six different copies.
--
-- THE SIX COPIES. The chip predicate was written out six times: twice here in
-- `browse_stats_properties` (include arm + exclude arm), twice in
-- `browse_map_cells`, once in `frontend/src/lib/queries.ts`
-- (`districtsFilterClause`, the PostgREST string) and once more in the same file
-- as an in-memory row predicate for the pipeline board (`matchesDistrictChip`),
-- plus `api/location_filter.district_where` for the Watchdog. Each carried the
-- same FIVE predicates: `obec_id`, `okres_id`, `region_id`, a `locality` pair
-- (obec id AND `place_search_text ILIKE`), and a legacy name fallback ILIKE-ing
-- across `district` / `place_search_text` / `okres` / `region` AND-ed with an
-- optional `context` narrow. Six copies x five predicates is how Browse, the
-- Stats tab, the map and the Watchdog came to disagree about what a chip means.
--
-- WHY EQUALITY IS ENOUGH NOW. `listing_location` (migration 501) answers every
-- listing with RUIAN codes and migration 503 published them on the serving
-- views; `browse_list.obec_id` / `okres_id` / `region_id` already WERE those
-- codes (`admin_boundaries.id` IS the RUIAN code -- migrations 083/141, and the
-- column comments on 162/171 say so), so for a resolved row this swap is
-- value-identical. What the ILIKE arms bought was chips with no code: those are
-- resolved by NAME once at read time now (`upgrade_district_chips`, against
-- `ruian_name_index`), not by substring-matching a display string.
--
-- THE NULL ARM IS THE SAFETY RULE, not an oversight. A chip that reaches the
-- RPC with no code matches NOTHING (`when admin_id is null then false`, and the
-- `else false` for an unknown level). An unresolvable INCLUDE chip therefore
-- contributes nothing to the cohort rather than silently widening it -- the
-- failure mode that matters is a Watchdog whose place filter quietly becomes
-- "the whole country" and mails the operator about it.
--
-- `cast_obce_id` IS THE FOURTH LEVEL. RUIAN draws no polygon for `cast_obce` or
-- `momc` (`location_data/ruian_boundaries.LAYERS` loads ten levels and neither
-- of those), so a quarter can never be point-in-polygon'd: `/maps/resolve`
-- places the obec from the point and the part BY NAME inside it
-- (`ruian_name_index`). The code it returns is matched here, and nowhere else
-- is a "quarter" inferred from text.
--
-- WHY properties_public GAINS ONLY THE ONE COLUMN. The Watchdog matcher runs
-- against `properties_public` and needs the fourth level. Its `obec_id` /
-- `okres_id` / `region_id` deliberately stay on their legacy source (the
-- trigger-289 columns on `properties`) in this step: those are the same numbers
-- as `listing_location`'s (see above), so the ONE predicate is honoured, and
-- re-sourcing them here would move the matcher's cohort in the same PR that
-- changes its predicate. W4 re-sources them with its own measurement. There is
-- no legacy twin for `cast_obce_id`, so that one can only come from
-- `listing_location`.
--
-- GRANTS. `create or replace function` keeps the existing ACL, so migration
-- 428's revoke on `browse_stats_properties` and migration 439's on
-- `browse_map_cells` survive untouched -- no DO block, nothing dynamic here.
-- The two views restate their grants because `create or replace view` is a
-- full re-declaration of the object.
--
-- ORDER OF APPLICATION: after 503. Every column this file reads
-- (`cast_obce_id` on `browse_list` / `properties_map_mv`, `display_label` on
-- `properties_public`) is one 503 appends, and 503's own gate (rule 25's
-- coverage invariant) applies unchanged.

begin;

set local lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 0. The column this file's predicate reads must already be there.
--
--    A plpgsql function body is NOT validated at CREATE time (`check_function_
--    bodies` is off for plpgsql's SQL statements), so `l.cast_obce_id` inside the
--    two RPCs below would be accepted here and fail at the FIRST browser read of
--    Browse's Stats tab or the map — as a 500 on a cohort read, which the SPA
--    swallows into an empty result. Migration 503 appends the column to
--    `browse_projection` and rebuilds both read models; assert it landed rather
--    than discovering it from a user's empty map.
-- ---------------------------------------------------------------------------

-- `pg_attribute`, not `information_schema.columns`: a MATERIALIZED VIEW has no
-- information_schema row at all (SQL-standard views only), so the catalog-free
-- spelling reports `properties_map_mv` as missing every column it has. Migration
-- 503's own assertions use this form for the same reason.

do $$
declare v_missing text;
begin
  select string_agg(t, ', ') into v_missing from (
    select 'browse_list' as t where not exists (
      select 1 from pg_attribute a
       where a.attrelid = 'public.browse_list'::regclass
         and a.attname = 'cast_obce_id' and not a.attisdropped)
    union all
    select 'properties_map_mv' where not exists (
      select 1 from pg_attribute a
       where a.attrelid = 'public.properties_map_mv'::regclass
         and a.attname = 'cast_obce_id' and not a.attisdropped)
  ) s;
  if v_missing is not null then
    raise exception
      'read model(s) % lack cast_obce_id -- apply migration 503 and let BOTH its '
      'rebuilds finish (a concurrent cron tick holding the advisory lock makes '
      'them skip) before applying 504', v_missing;
  end if;
end
$$;

-- ---------------------------------------------------------------------------
-- 1. properties_public -- migration 503's body plus `cast_obce_id`.
--    The Watchdog matcher's relation; the fourth chip level has to exist here
--    or a `cast_obce` chip would silently match nothing on that ONE surface.
-- ---------------------------------------------------------------------------

create or replace view properties_public as
select
    p.id as property_id,
    p.repr_listing_id as sreality_id,
    p.first_seen_at,
    p.last_seen_at,
    p.is_active,
    p.category_main,
    p.category_type,
    p.current_price_czk as price_czk,
    l.price_unit,
    p.area_m2,
    p.disposition,
    p.locality,
    p.district,
    p.locality_district_id,
    p.locality_region_id,
    p.lat,
    p.lng,
    l.floor,
    l.total_floors,
    p.has_balcony,
    p.has_parking,
    p.has_lift,
    p.building_type,
    p.condition,
    p.energy_rating,
    p.estate_area,
    p.usable_area,
    p.garden_area,
    p.category_sub_cb,
    p.furnished,
    p.terrace,
    p.cellar,
    p.garage,
    p.parking_lots,
    p.ownership,
    l.broker_name,
    null::text as broker_email,
    null::text as broker_phone,
    case
        when p.is_active then greatest(0, floor(extract(epoch from now() - p.first_seen_at) / 86400::numeric)::integer)
        else greatest(0, floor(extract(epoch from p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
    end as tom_days,
    measure_price_per_m2(p.current_price_czk::numeric, p.area_m2, p.category_main, p.category_type) as price_per_m2,
    p.building_condition_level,
    p.apartment_condition_level,
    l.description,
    p.source_count,
    p.distinct_site_count,
    p.price_drop_count,
    p.price_rise_count,
    p.max_price_drop_pct,
    p.stats_computed_at,
    p.source,
    coalesce(p.street, l.street) as street,
    p.mf_reference_rent_czk,
    p.mf_gross_yield_pct,
    p.obec,
    p.okres,
    p.region,
    p.home_obec_pop,
    p.near_pop_5km,
    p.near_pop_15km,
    p.near_jobs_5km,
    p.near_jobs_15km,
    p.near_youth_5km,
    p.near_youth_15km,
    p.near_overall_5km,
    p.near_overall_15km,
    p.subtype,
    p.last_change_at,
    p.obec_id,
    p.okres_id,
    p.region_id,
    p.price_change_count,
    p.price_change_count_30d,
    p.price_change_count_90d,
    p.price_change_count_365d,
    p.total_price_change_pct,
    concat_ws(', '::text, p.street, p.locality) as place_search_text,
    p.asset_id,
    p.mf_reference_rent,
    p.published_at,
    p.repr_listing_ref_id as listing_id,
    l.source_id_native,
    p.home_city_id,
    measure_price_per_m2_basis(p.category_main, p.category_type) as price_per_m2_basis,
    -- ---- appended by migration 503 (W3 S1) ----
    location_display_label(ll.street_name, ll.house_number_cp, ll.house_number_co,
                           ll.obec_name, ll.cast_obce_name, ll.country_code,
                           ll.country_status) as display_label,
    -- ---- appended by migration 504 (W3 S3) ----
    -- The fourth chip level. There is no legacy twin to re-source from: no
    -- trigger ever wrote a part-of-municipality code onto `listings`, so this
    -- column exists only because `listing_location` answers it.
    ll.cast_obce_kod as cast_obce_id
from properties p
     left join listings l on l.id = p.repr_listing_ref_id
     left join listing_location ll on ll.listing_id = p.repr_listing_ref_id
where p.status = 'active'::text;

revoke all on properties_public from anon;
grant select on properties_public to authenticated;

-- ---------------------------------------------------------------------------
-- 2. pipeline_board_public -- migration 503's body plus `cast_obce_id`.
--    The kanban loads its cards whole and filters them in the browser
--    (`matchesDistricts`, rule 22), so the board needs the same four codes the
--    server-side predicate uses or the two would disagree on the same chip.
-- ---------------------------------------------------------------------------

create or replace view pipeline_board_public
with (security_invoker = true) as
select
  pp.property_id,
  pp.stage_id,
  pp.board_position,
  pp.entered_stage_at,
  pp.added_at,
  p.sreality_id,
  p.source,
  p.source_id_native,
  p.listing_id,
  p.category_main,
  p.street,
  p.district,
  p.disposition,
  p.subtype,
  p.area_m2,
  p.price_czk,
  p.mf_gross_yield_pct,
  p.total_price_change_pct,
  p.price_change_count,
  p.obec_id,
  p.okres_id,
  p.region_id,
  p.place_search_text,
  p.obec,
  p.locality,
  p.okres,
  p.region,
  p.is_active,
  p.category_type,
  p.price_per_m2,
  p.price_per_m2_basis,
  -- ---- appended by migration 503 (W3 S1) ----
  p.display_label,
  -- ---- appended by migration 504 (W3 S3) ----
  p.cast_obce_id
from property_pipeline_public pp
left join properties_public p on p.property_id = pp.property_id;

revoke all on pipeline_board_public from public, anon;
grant select on pipeline_board_public to authenticated;

-- ---------------------------------------------------------------------------
-- 3. browse_stats_properties -- migration 436's body, chip predicate replaced.
--    Nothing else in the 74-parameter signature changes: the parameter list is
--    the SPA's `buildBrowseStatsArgs` contract (tests/test_browse_map_read_
--    contract.py pins builder-key = RPC-parameter both ways), so
--    `districts_context_filter` stays a parameter even though the body no
--    longer reads it -- dropping it would need a DROP FUNCTION and would break
--    every deployed bundle mid-rollout. It is now inert, and W4 removes it from
--    both RPCs and the builder in one step.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.browse_stats_properties(districts_filter text[] DEFAULT NULL::text[], dispositions_filter text[] DEFAULT NULL::text[], price_min_filter integer DEFAULT NULL::integer, price_max_filter integer DEFAULT NULL::integer, area_min_filter integer DEFAULT NULL::integer, area_max_filter integer DEFAULT NULL::integer, active_only_filter boolean DEFAULT false, last_seen_min_days integer DEFAULT NULL::integer, last_seen_max_days integer DEFAULT NULL::integer, first_seen_min_days integer DEFAULT NULL::integer, first_seen_max_days integer DEFAULT NULL::integer, tom_days_min integer DEFAULT NULL::integer, tom_days_max integer DEFAULT NULL::integer, has_balcony_filter boolean DEFAULT NULL::boolean, has_lift_filter boolean DEFAULT NULL::boolean, has_parking_filter boolean DEFAULT NULL::boolean, inactive_only_filter boolean DEFAULT false, furnished_filter text[] DEFAULT NULL::text[], terrace_filter boolean DEFAULT NULL::boolean, cellar_filter boolean DEFAULT NULL::boolean, garage_filter boolean DEFAULT NULL::boolean, category_sub_cb_filter integer DEFAULT NULL::integer, building_type_filter text[] DEFAULT NULL::text[], tag_ids bigint[] DEFAULT NULL::bigint[], category_main_filter text[] DEFAULT NULL::text[], category_type_filter text DEFAULT NULL::text, bbox_west double precision DEFAULT NULL::double precision, bbox_south double precision DEFAULT NULL::double precision, bbox_east double precision DEFAULT NULL::double precision, bbox_north double precision DEFAULT NULL::double precision, ownership_filter text[] DEFAULT NULL::text[], estate_area_min_filter double precision DEFAULT NULL::double precision, estate_area_max_filter double precision DEFAULT NULL::double precision, usable_area_min_filter double precision DEFAULT NULL::double precision, usable_area_max_filter double precision DEFAULT NULL::double precision, parking_lots_min_filter integer DEFAULT NULL::integer, garden_area_min_filter double precision DEFAULT NULL::double precision, garden_area_max_filter double precision DEFAULT NULL::double precision, condition_match_filter text[] DEFAULT NULL::text[], districts_context_filter text[] DEFAULT NULL::text[], city_index_rules jsonb DEFAULT NULL::jsonb, city_pop_min integer DEFAULT NULL::integer, city_pop_max integer DEFAULT NULL::integer, city_proximity jsonb DEFAULT NULL::jsonb, price_per_m2_min double precision DEFAULT NULL::double precision, price_per_m2_max double precision DEFAULT NULL::double precision, portal_filter text[] DEFAULT NULL::text[], mf_gross_yield_pct_min double precision DEFAULT NULL::double precision, mf_gross_yield_pct_max double precision DEFAULT NULL::double precision, near_pop_5km_min integer DEFAULT NULL::integer, near_pop_15km_min integer DEFAULT NULL::integer, near_jobs_5km_min double precision DEFAULT NULL::double precision, near_jobs_15km_min double precision DEFAULT NULL::double precision, near_youth_5km_min double precision DEFAULT NULL::double precision, near_youth_15km_min double precision DEFAULT NULL::double precision, near_overall_5km_min double precision DEFAULT NULL::double precision, near_overall_15km_min double precision DEFAULT NULL::double precision, districts_excluded_filter boolean[] DEFAULT NULL::boolean[], subtype_filter text[] DEFAULT NULL::text[], recently_added_days integer DEFAULT NULL::integer, recently_changed_days integer DEFAULT NULL::integer, obec_ids_filter bigint[] DEFAULT NULL::bigint[], districts_levels text[] DEFAULT NULL::text[], districts_ids bigint[] DEFAULT NULL::bigint[], building_condition_level_min integer DEFAULT NULL::integer, building_condition_level_max integer DEFAULT NULL::integer, apartment_condition_level_min integer DEFAULT NULL::integer, apartment_condition_level_max integer DEFAULT NULL::integer, price_change_count_min integer DEFAULT NULL::integer, price_change_window_days integer DEFAULT NULL::integer, total_price_change_pct_filter double precision DEFAULT NULL::double precision, with_estimates boolean DEFAULT false, include_no_price boolean DEFAULT false, property_ids_filter bigint[] DEFAULT NULL::bigint[])
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
begin
  if city_proximity is not null then
    raise exception using errcode = '22023',
      message = 'browse_stats_properties: city_proximity is retired (W5, migration 436). '
                'Use the migration-142 near_*_min columns.';
  end if;
  return (
  with filtered as (
    select l.sreality_id, l.first_seen_at, l.last_seen_at, l.is_active, l.price_czk, l.area_m2, l.disposition, l.tom_days, l.price_per_m2, l.category_main, l.category_type
    from browse_list l
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
      and (category_main_filter   is null or array_length(category_main_filter, 1) is null or l.category_main = any(category_main_filter))
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
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)])),
                 coalesce(districts_levels, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_ids, array_fill(null::bigint, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, excl, lvl, admin_id, ord)
          where not coalesce(excl, false)
            and case
              -- ONE code predicate (W3 S3): a chip is a level plus a RUIAN code,
              -- matched with plain equality. No ILIKE, no place_search_text, no
              -- context narrow. A chip with no code matches NOTHING (the `else`
              -- and the null guard) -- a saved filter we cannot resolve must
              -- never widen a cohort.
              when admin_id is null                  then false
              when lvl = 'kraj'                      then l.region_id    = admin_id
              when lvl = 'okres'                     then l.okres_id     = admin_id
              when lvl = 'obec'                      then l.obec_id      = admin_id
              when lvl = 'cast_obce'                 then l.cast_obce_id = admin_id
              -- a street / POI / address pick carries its containing obec code
              when lvl = 'locality'                  then l.obec_id      = admin_id
              else false
            end
        )
      )
      and (
        districts_filter is null or array_length(districts_filter, 1) is null
        or not exists (
          select 1 from unnest(districts_filter,
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)])),
                 coalesce(districts_levels, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_ids, array_fill(null::bigint, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, excl, lvl, admin_id, ord)
          where coalesce(excl, false)
            and case
              -- ONE code predicate (W3 S3): a chip is a level plus a RUIAN code,
              -- matched with plain equality. No ILIKE, no place_search_text, no
              -- context narrow. A chip with no code matches NOTHING (the `else`
              -- and the null guard) -- a saved filter we cannot resolve must
              -- never widen a cohort.
              when admin_id is null                  then false
              when lvl = 'kraj'                      then l.region_id    = admin_id
              when lvl = 'okres'                     then l.okres_id     = admin_id
              when lvl = 'obec'                      then l.obec_id      = admin_id
              when lvl = 'cast_obce'                 then l.cast_obce_id = admin_id
              -- a street / POI / address pick carries its containing obec code
              when lvl = 'locality'                  then l.obec_id      = admin_id
              else false
            end
        )
      )
      and (dispositions_filter    is null or l.disposition     = any(dispositions_filter))
      and (price_min_filter       is null or (include_no_price and l.price_czk is null) or l.price_czk >= price_min_filter)
      and (price_max_filter       is null or (include_no_price and l.price_czk is null) or l.price_czk <= price_max_filter)
      and (area_min_filter        is null or l.area_m2        >= area_min_filter)
      and (area_max_filter        is null or l.area_m2        <= area_max_filter)
      and (price_per_m2_min is null or l.price_per_m2 >= price_per_m2_min)
      and (price_per_m2_max is null or l.price_per_m2 <= price_per_m2_max)
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
      and (property_ids_filter is null or l.property_id = any(property_ids_filter))
      and (tag_ids is null or array_length(tag_ids, 1) is null or l.property_id in (
          select pt.property_id from property_tags pt where pt.tag_id = any(tag_ids)
          group by pt.property_id having count(distinct pt.tag_id) = array_length(tag_ids, 1)))
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
      and (city_index_rules is null or jsonb_array_length(city_index_rules) = 0
           or l.obec_id = any (array(select curated_cities_matching(city_index_rules))))
      and true
  ),
  price_pct as (select percentile_cont(0.25) within group (order by price_czk)::int as p25, percentile_cont(0.50) within group (order by price_czk)::int as p50, percentile_cont(0.75) within group (order by price_czk)::int as p75 from filtered where price_czk is not null),
  ppm2_pct as (select percentile_cont(0.25) within group (order by price_per_m2)::int as p25, percentile_cont(0.50) within group (order by price_per_m2)::int as p50, percentile_cont(0.75) within group (order by price_per_m2)::int as p75 from filtered where price_per_m2 is not null),
  ppm2_basis as (select case when count(distinct b) = 0 then null when count(distinct b) = 1 then min(b) else 'mixed' end as basis from (select measure_price_per_m2_basis(category_main, category_type) as b from filtered where price_per_m2 is not null) t),
  disposition_dist as (select coalesce(disposition, 'unspecified') as disposition, count(*)::int as n, count(price_per_m2)::int as ppm2_n, min(price_per_m2)::int as ppm2_min, percentile_cont(0.25) within group (order by price_per_m2)::int as ppm2_p25, percentile_cont(0.50) within group (order by price_per_m2)::int as ppm2_median, percentile_cont(0.75) within group (order by price_per_m2)::int as ppm2_p75, max(price_per_m2)::int as ppm2_max from filtered group by disposition order by n desc, disposition asc),
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
    'ppm2_basis', (select basis from ppm2_basis),
    'dispositions', coalesce((select jsonb_agg(jsonb_build_object('disposition', disposition, 'n', n, 'ppm2_box', case when ppm2_n > 0 then jsonb_build_object('n', ppm2_n, 'min', ppm2_min, 'p25', ppm2_p25, 'median', ppm2_median, 'p75', ppm2_p75, 'max', ppm2_max) else null end)) from disposition_dist), '[]'::jsonb),
    'price_band_velocity', coalesce((select jsonb_agg(jsonb_build_object('bucket', bs.bucket, 'p_lo', bs.p_lo, 'p_hi', bs.p_hi, 'n', bs.n, 'pct_share', case when bs.priced_total is null or bs.priced_total = 0 then null else round(bs.n * 100.0 / bs.priced_total, 1) end, 'price_min', bs.price_min, 'price_max', bs.price_max, 'tom_box', case when bs.tom_n > 0 then jsonb_build_object('n', bs.tom_n, 'min', bs.tom_min, 'p25', round(bs.tom_p25::numeric, 1), 'median', round(bs.tom_median::numeric, 1), 'mean', round(bs.tom_mean::numeric, 1), 'p75', round(bs.tom_p75::numeric, 1), 'max', bs.tom_max) else null end) order by bs.p_lo) from band_stats bs), '[]'::jsonb)
  )
  );
end
$function$;

-- ---------------------------------------------------------------------------
-- 4. browse_map_cells -- migration 439's body, the same two replacements.
--    The map and the Stats tab answer for ONE cohort; that is only true while
--    these two bodies carry the same predicate, which is why they are in one
--    migration.
-- ---------------------------------------------------------------------------

create or replace function public.browse_map_cells(
  districts_filter text[] default null::text[],
  dispositions_filter text[] default null::text[],
  price_min_filter integer default null::integer,
  price_max_filter integer default null::integer,
  area_min_filter integer default null::integer,
  area_max_filter integer default null::integer,
  active_only_filter boolean default false,
  last_seen_min_days integer default null::integer,
  last_seen_max_days integer default null::integer,
  first_seen_min_days integer default null::integer,
  first_seen_max_days integer default null::integer,
  tom_days_min integer default null::integer,
  tom_days_max integer default null::integer,
  has_balcony_filter boolean default null::boolean,
  has_lift_filter boolean default null::boolean,
  has_parking_filter boolean default null::boolean,
  inactive_only_filter boolean default false,
  furnished_filter text[] default null::text[],
  terrace_filter boolean default null::boolean,
  cellar_filter boolean default null::boolean,
  garage_filter boolean default null::boolean,
  category_sub_cb_filter integer default null::integer,
  building_type_filter text[] default null::text[],
  tag_ids bigint[] default null::bigint[],
  category_main_filter text[] default null::text[],
  category_type_filter text default null::text,
  bbox_west double precision default null::double precision,
  bbox_south double precision default null::double precision,
  bbox_east double precision default null::double precision,
  bbox_north double precision default null::double precision,
  ownership_filter text[] default null::text[],
  estate_area_min_filter double precision default null::double precision,
  estate_area_max_filter double precision default null::double precision,
  usable_area_min_filter double precision default null::double precision,
  usable_area_max_filter double precision default null::double precision,
  parking_lots_min_filter integer default null::integer,
  garden_area_min_filter double precision default null::double precision,
  garden_area_max_filter double precision default null::double precision,
  condition_match_filter text[] default null::text[],
  districts_context_filter text[] default null::text[],
  city_index_rules jsonb default null::jsonb,
  city_pop_min integer default null::integer,
  city_pop_max integer default null::integer,
  city_proximity jsonb default null::jsonb,
  price_per_m2_min double precision default null::double precision,
  price_per_m2_max double precision default null::double precision,
  portal_filter text[] default null::text[],
  mf_gross_yield_pct_min double precision default null::double precision,
  mf_gross_yield_pct_max double precision default null::double precision,
  near_pop_5km_min integer default null::integer,
  near_pop_15km_min integer default null::integer,
  near_jobs_5km_min double precision default null::double precision,
  near_jobs_15km_min double precision default null::double precision,
  near_youth_5km_min double precision default null::double precision,
  near_youth_15km_min double precision default null::double precision,
  near_overall_5km_min double precision default null::double precision,
  near_overall_15km_min double precision default null::double precision,
  districts_excluded_filter boolean[] default null::boolean[],
  subtype_filter text[] default null::text[],
  recently_added_days integer default null::integer,
  recently_changed_days integer default null::integer,
  obec_ids_filter bigint[] default null::bigint[],
  districts_levels text[] default null::text[],
  districts_ids bigint[] default null::bigint[],
  building_condition_level_min integer default null::integer,
  building_condition_level_max integer default null::integer,
  apartment_condition_level_min integer default null::integer,
  apartment_condition_level_max integer default null::integer,
  price_change_count_min integer default null::integer,
  price_change_window_days integer default null::integer,
  total_price_change_pct_filter double precision default null::double precision,
  with_estimates boolean default false,
  include_no_price boolean default false,
  property_ids_filter bigint[] default null::bigint[],
  -- The two parameters browse_stats_properties does not have.
  --
  -- listing_ids_filter: the SPA's applyPrefilters emits `.in()` on THREE id
  -- spaces -- listing_id, obec_id and property_id. browse_stats_properties
  -- carries only the last two because the legacy city-quality path reaches it
  -- as city_index_rules instead. The map resolves that path client-side into a
  -- listing_id allowlist (queries.ts resolveCityQualityPrefilterLegacy, live
  -- whenever ?cityQualityLegacy=1 is remembered in localStorage), so without
  -- this parameter the RPC would silently drop a prefilter that the read it
  -- replaces applies.
  listing_ids_filter bigint[] default null::bigint[],
  -- Above this many mappable rows the answer is cells; at or below it the
  -- caller re-reads the cohort as points. Measured: a 2,000-row point read is
  -- 153 blocks and ~906 KB, which is the ceiling this threshold buys.
  point_budget integer default 2000
)
returns jsonb
language plpgsql
stable
-- 74 mostly-NULL parameters: a generic plan would pick one shape and use it for
-- every cohort. Same setting, same reason, as browse_stats_properties.
set plan_cache_mode to 'force_custom_plan'
as $function$
declare
  -- The Czech Republic, used only when the cohort carries no bbox of its own.
  -- Real CZ bounds are lng 12.09..18.86, lat 48.55..51.06.
  c_cz_west  constant double precision := 12.0;
  c_cz_east  constant double precision := 18.9;
  c_cz_south constant double precision := 48.5;
  c_cz_north constant double precision := 51.1;
  c_cols constant integer := 20;
  c_rows constant integer := 13;
  v_w double precision;
  v_e double precision;
  v_s double precision;
  v_n double precision;
  v_cw double precision;
  v_ch double precision;
  v_result jsonb;
  v_total bigint;
begin
  if city_proximity is not null then
    raise exception using errcode = '22023',
      message = 'browse_map_cells: city_proximity is retired (W5, migration 436). '
                'Use the migration-142 near_*_min columns.';
  end if;

  v_w := coalesce(bbox_west,  c_cz_west);
  v_e := coalesce(bbox_east,  c_cz_east);
  v_s := coalesce(bbox_south, c_cz_south);
  v_n := coalesce(bbox_north, c_cz_north);
  -- A degenerate extent (a point bbox, or an inverted one from a bad URL) would
  -- divide by zero or produce a negative cell; fall back to the CZ box rather
  -- than raise, because the cohort predicate is still perfectly well defined.
  if not (v_e > v_w and v_n > v_s) then
    v_w := c_cz_west; v_e := c_cz_east; v_s := c_cz_south; v_n := c_cz_north;
  end if;
  v_cw := (v_e - v_w) / c_cols;
  v_ch := (v_n - v_s) / c_rows;

  with g as (
    select
      -- cx/cy are NULL together for a row outside the grid extent. Those rows
      -- collapse into ONE group, are reported as off_grid, and are still inside
      -- total -- never relocated onto an edge cell, never dropped.
      case when l.lat >= v_s and l.lat <= v_n and l.lng >= v_w and l.lng <= v_e
           then least(floor((l.lng - v_w) / v_cw)::int, c_cols - 1) end as cx,
      case when l.lat >= v_s and l.lat <= v_n and l.lng >= v_w and l.lng <= v_e
           then least(floor((l.lat - v_s) / v_ch)::int, c_rows - 1) end as cy,
      count(*)::int as n,
      -- The MEAN of the points in the cell, not the cell centre: the bubble
      -- then sits on the settlement instead of on a lattice vertex. Both
      -- columns are index keys, so this costs nothing and keeps Heap Fetches 0.
      avg(l.lat) as la,
      avg(l.lng) as lo
    from properties_map_mv l
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
      and (category_main_filter   is null or array_length(category_main_filter, 1) is null or l.category_main = any(category_main_filter))
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
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)])),
                 coalesce(districts_levels, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_ids, array_fill(null::bigint, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, excl, lvl, admin_id, ord)
          where not coalesce(excl, false)
            and case
              -- ONE code predicate (W3 S3): a chip is a level plus a RUIAN code,
              -- matched with plain equality. No ILIKE, no place_search_text, no
              -- context narrow. A chip with no code matches NOTHING (the `else`
              -- and the null guard) -- a saved filter we cannot resolve must
              -- never widen a cohort.
              when admin_id is null                  then false
              when lvl = 'kraj'                      then l.region_id    = admin_id
              when lvl = 'okres'                     then l.okres_id     = admin_id
              when lvl = 'obec'                      then l.obec_id      = admin_id
              when lvl = 'cast_obce'                 then l.cast_obce_id = admin_id
              -- a street / POI / address pick carries its containing obec code
              when lvl = 'locality'                  then l.obec_id      = admin_id
              else false
            end
        )
      )
      and (
        districts_filter is null or array_length(districts_filter, 1) is null
        or not exists (
          select 1 from unnest(districts_filter,
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)])),
                 coalesce(districts_levels, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_ids, array_fill(null::bigint, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, excl, lvl, admin_id, ord)
          where coalesce(excl, false)
            and case
              -- ONE code predicate (W3 S3): a chip is a level plus a RUIAN code,
              -- matched with plain equality. No ILIKE, no place_search_text, no
              -- context narrow. A chip with no code matches NOTHING (the `else`
              -- and the null guard) -- a saved filter we cannot resolve must
              -- never widen a cohort.
              when admin_id is null                  then false
              when lvl = 'kraj'                      then l.region_id    = admin_id
              when lvl = 'okres'                     then l.okres_id     = admin_id
              when lvl = 'obec'                      then l.obec_id      = admin_id
              when lvl = 'cast_obce'                 then l.cast_obce_id = admin_id
              -- a street / POI / address pick carries its containing obec code
              when lvl = 'locality'                  then l.obec_id      = admin_id
              else false
            end
        )
      )
      and (dispositions_filter    is null or l.disposition     = any(dispositions_filter))
      and (price_min_filter       is null or (include_no_price and l.price_czk is null) or l.price_czk >= price_min_filter)
      and (price_max_filter       is null or (include_no_price and l.price_czk is null) or l.price_czk <= price_max_filter)
      and (area_min_filter        is null or l.area_m2        >= area_min_filter)
      and (area_max_filter        is null or l.area_m2        <= area_max_filter)
      and (price_per_m2_min is null or l.price_per_m2 >= price_per_m2_min)
      and (price_per_m2_max is null or l.price_per_m2 <= price_per_m2_max)
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
      and (property_ids_filter is null or l.property_id = any(property_ids_filter))
      -- The third id space (see the parameter's comment). browse_list's twin has
      -- no equivalent; the map's does, and must.
      and (listing_ids_filter is null or l.listing_id = any(listing_ids_filter))
      and (tag_ids is null or array_length(tag_ids, 1) is null or l.property_id in (
          select pt.property_id from property_tags pt where pt.tag_id = any(tag_ids)
          group by pt.property_id having count(distinct pt.tag_id) = array_length(tag_ids, 1)))
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
      and (city_index_rules is null or jsonb_array_length(city_index_rules) = 0
           or l.obec_id = any (array(select curated_cities_matching(city_index_rules))))
      and true
    group by 1, 2
  )
  select jsonb_build_object(
    'total',        coalesce((select sum(n) from g), 0),
    'off_grid',     coalesce((select sum(n) from g where cx is null), 0),
    'cell_lat_deg', v_ch,
    'cell_lng_deg', v_cw,
    'grid_west',  v_w, 'grid_east',  v_e,
    'grid_south', v_s, 'grid_north', v_n,
    'cells', coalesce(
      (select jsonb_agg(jsonb_build_object('lat', la, 'lng', lo, 'n', n) order by n desc)
         from g where cx is not null),
      '[]'::jsonb)
  )
  into v_result;

  v_total := (v_result->>'total')::bigint;
  -- At or below the budget the caller re-reads the cohort as points, so the
  -- cells are withheld rather than shipped-and-ignored. `clustered` is what the
  -- caller branches on; it is never inferred from the array being empty (an
  -- empty cohort is not a small one).
  return v_result || jsonb_build_object(
    'clustered', v_total > point_budget,
    'cells', case when v_total > point_budget then v_result->'cells' else 'null'::jsonb end
  );
end
$function$;

commit;

-- PostgREST caches function signatures; the bodies changed, not the signatures,
-- but the two view re-creations above do change the schema cache.
select pg_notify('pgrst', 'reload schema');
