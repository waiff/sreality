-- 374_home_city_precompute.sql
--
-- Migration 373 fixed listings_with_city_quality's RLS lockout (BUG1) by
-- repointing FROM listings -> FROM browse_list, which is correct but
-- exposed a SECOND, pre-existing defect: evaluating ST_Covers(admin
-- boundary) / ST_DWithin(centroid, radius) against all 206 curated cities,
-- live, per browse_list row, for a rule-bearing call. Live EXPLAIN on a
-- single real rule ("bezpecnost >= 0"): estimated cost ~1.8 BILLION even
-- after materializing the city_index_values_public "latest revision"
-- lookup (which itself re-executed a 13,563-row aggregate scan per
-- (row x city x rule) triple before that fix) -- the query does not
-- complete. Root cause: `browse_list` stores plain lat/lng floats, not an
-- indexed geometry column, so there is no way for a per-request join to use
-- a spatial index from the point side; the naive fix trades BUG1's silent
-- empty result for an unusable timeout, which is not actually fixed.
--
-- This was never caught before because BUG1 has made every real
-- authenticated call return instantly-empty (via the RLS lockout) since
-- Phase 1 shipped, and it is unclear the RPC was ever exercised at full
-- production scale before that either.
--
-- Fix (mirrors the ESTABLISHED precedent for exactly this problem,
-- migration 142's home_obec_pop/near_* columns + recompute_city_proximity()):
-- precompute which curated city (if any) a property's coordinate belongs
-- to, ONCE, incrementally, off the request path -- not per Browse query.
-- `properties.geom` already carries a GiST index (migration 091), so the
-- backfill itself is a cheap small-anchor-set spatial join (same shape as
-- recompute_city_proximity: ~2ms/property). Every consumer then becomes a
-- plain indexed equality join instead of a live spatial containment test.
--
-- Tie-break when a property's point could satisfy more than one curated
-- city (RUIAN obec polygons never overlap each other, so at most one
-- boundary-covers match is possible; only the radius-fallback cities -- the
-- ones with no admin_boundary_id -- can genuinely overlap): an exact
-- boundary-covers match always wins over a radius-fallback match, and among
-- radius-fallback candidates the NEAREST centroid wins. This mirrors the
-- existing predicate's own precedence (ST_Covers checked before the
-- ST_DWithin fallback) rather than inventing new semantics.
--
-- This ALSO permanently closes the CLAUDE.md rule-16 divergence flagged in
-- migration 373's header: listings_with_city_quality, browse_stats_properties,
-- and toolkit.comparables._city_quality_clauses (Watchdog) now all join the
-- SAME home_city_id column instead of each re-implementing (and inevitably
-- drifting on) the geo containment test.
--
-- The city_proximity / near_city_proximity branch is UNCHANGED (still a
-- live ST_DWithin spatial join, both here and in browse_stats_properties):
-- confirmed dead in the frontend today (no widget ever sets
-- nearCityProximity -- CityIndexRulesPicker only wires city_index_rules),
-- so it carries no live production traffic and is not the bottleneck this
-- migration addresses. It is NOT precomputable the same way regardless,
-- since the radius is chosen by the operator at query time, not fixed.
-- Left correct but unoptimized; flagged here so a future session does not
-- assume it was load-tested.

set local lock_timeout = '5s';

alter table properties
  add column if not exists home_city_id bigint references curated_cities(id),
  add column if not exists home_city_computed_at timestamptz;

comment on column properties.home_city_id is
  'The curated city (if any) whose polygon covers this property, or whose '
  'radius fallback reaches it. Precomputed by recompute_home_city() -- '
  'migration 374 -- so listings_with_city_quality / browse_stats_properties / '
  'Watchdog''s _city_quality_clauses share one indexed join instead of each '
  'running a live ST_Covers/ST_DWithin containment test.';

create index if not exists properties_home_city_id_idx
  on properties (home_city_id) where home_city_id is not null;

create or replace function recompute_home_city(p_full boolean default false)
returns integer
language plpgsql
as $$
declare
  n integer;
begin
  -- Anchor sets: the 206 curated cities, split by which containment test
  -- applies (mirrors migration 142's _prox_anchors pattern -- a tiny
  -- GiST-indexed temp set keeps the per-property probe cheap instead of a
  -- live join against the full admin_boundaries/curated_cities tables).
  drop table if exists _home_city_boundary_anchors;
  create temp table _home_city_boundary_anchors on commit drop as
  select cc.id as city_id, ab.geom as boundary_geom
  from curated_cities cc
  join admin_boundaries ab on ab.id = cc.admin_boundary_id
  where cc.admin_boundary_id is not null;
  create index on _home_city_boundary_anchors using gist (boundary_geom);

  drop table if exists _home_city_radius_anchors;
  create temp table _home_city_radius_anchors on commit drop as
  select cc.id as city_id, cc.centroid, cc.default_radius_m
  from curated_cities cc
  where cc.admin_boundary_id is null;
  create index on _home_city_radius_anchors using gist (centroid);

  analyze _home_city_boundary_anchors;
  analyze _home_city_radius_anchors;

  update properties p set
    home_city_id = s.city_id,
    home_city_computed_at = now()
  from (
    select p2.id, coalesce(cov.city_id, rad.city_id) as city_id
    from properties p2
    left join lateral (
      select ba.city_id from _home_city_boundary_anchors ba
      where st_covers(ba.boundary_geom, p2.geom)
      limit 1
    ) cov on true
    left join lateral (
      select ra.city_id from _home_city_radius_anchors ra
      where st_dwithin(p2.geom, ra.centroid, ra.default_radius_m)
      order by p2.geom <-> ra.centroid
      limit 1
    ) rad on cov.city_id is null
    where p2.geom is not null
      and (p_full or p2.home_city_computed_at is null)
  ) s
  where p.id = s.id;

  get diagnostics n = row_count;
  return n;
end
$$;

comment on function recompute_home_city(boolean) is
  'Fill properties.home_city_id from the curated-city ST_Covers/ST_DWithin '
  'containment test. Incremental (home_city_computed_at IS NULL) unless '
  'p_full. Run by recompute_home_city.yml (hourly) and after a '
  'curated_cities/admin boundary change (workflow_dispatch full=true).';

-- Expose on properties_public (Watchdog's _city_quality_clauses reads this
-- view) -- appended at the end, verbatim reproduction of migration 343's
-- body otherwise (CREATE OR REPLACE VIEW can only append columns).
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
    l.broker_email,
    l.broker_phone,
    case
        when p.is_active then greatest(0, floor(extract(epoch from now() - p.first_seen_at) / 86400::numeric)::integer)
        else greatest(0, floor(extract(epoch from p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
    end as tom_days,
    case
        when p.area_m2 is not null and p.area_m2 > 0::numeric and p.current_price_czk is not null then round(p.current_price_czk::numeric / p.area_m2, 2)
        else null::numeric
    end as price_per_m2,
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
    p.home_city_id
from properties p
     left join listings l on l.id = p.repr_listing_ref_id
where p.status = 'active'::text
  and (not (select publication_gate_enabled()) or p.published_at is not null);

-- Expose on browse_projection (Browse's two RPCs read browse_list, built
-- from `select * from browse_projection`) -- appended at the end, verbatim
-- reproduction of migration 363's body otherwise.
create or replace view browse_projection as
select
    id as property_id,
    repr_listing_id as sreality_id,
    first_seen_at,
    last_seen_at,
    is_active,
    category_main,
    category_type,
    current_price_czk as price_czk,
    area_m2,
    disposition,
    locality,
    district,
    locality_district_id,
    locality_region_id,
    lat,
    lng,
    has_balcony,
    has_parking,
    has_lift,
    building_type,
    condition,
    energy_rating,
    estate_area,
    usable_area,
    garden_area,
    category_sub_cb,
    furnished,
    terrace,
    cellar,
    garage,
    parking_lots,
    ownership,
    case
        when is_active then greatest(0, floor(extract(epoch from now() - first_seen_at) / 86400::numeric)::integer)
        else greatest(0, floor(extract(epoch from last_seen_at - first_seen_at) / 86400::numeric)::integer)
    end as tom_days,
    case
        when area_m2 is not null and area_m2 > 0::numeric and current_price_czk is not null then round(current_price_czk::numeric / area_m2, 2)
        else null::numeric
    end as price_per_m2,
    building_condition_level,
    apartment_condition_level,
    source,
    street,
    mf_reference_rent_czk,
    mf_gross_yield_pct,
    obec,
    okres,
    region,
    home_obec_pop,
    near_pop_5km,
    near_pop_15km,
    near_jobs_5km,
    near_jobs_15km,
    near_youth_5km,
    near_youth_15km,
    near_overall_5km,
    near_overall_15km,
    subtype,
    last_change_at,
    obec_id,
    okres_id,
    region_id,
    price_change_count,
    price_change_count_30d,
    price_change_count_90d,
    price_change_count_365d,
    total_price_change_pct,
    concat_ws(', '::text, street, locality) as place_search_text,
    asset_id,
    repr_listing_ref_id as listing_id,
    (select l.source_id_native from listings l where l.id = p.repr_listing_ref_id) as source_id_native,
    -- all_sources/active_sources: live on browse_projection already (an
    -- in-flight, not-yet-merged-to-main branch applied them directly via
    -- MCP -- CREATE OR REPLACE VIEW is append-only, so they must be
    -- reproduced here or Postgres reads this as dropping them). Orthogonal
    -- to this migration; reproduced verbatim from the live view definition.
    all_sources,
    active_sources,
    home_city_id
from properties p
where status = 'active'::text
  and (not (select publication_gate_enabled()) or published_at is not null);

-- Propagate the new column into the browse_list TABLE now, before the two
-- functions below are defined: LANGUAGE SQL functions are parsed (and their
-- column references resolved) at CREATE TIME, not just on first call, so
-- listings_with_city_quality would fail to even compile against browse_list's
-- pre-rebuild column set otherwise. Mirrors migration 363's same ordering.
select rebuild_browse_list();
select rebuild_properties_map_mv();

-- listings_with_city_quality: the city_index_rules branch now joins
-- l.home_city_id (indexed equality) instead of a live ST_Covers/ST_DWithin
-- scan against all 206 curated cities per row. city_proximity is unchanged
-- (see header -- confirmed dead in the frontend, kept correct not optimized).
create or replace function listings_with_city_quality(
  p_index_rules jsonb default null,
  p_pop_min     int   default null,
  p_pop_max     int   default null,
  p_proximity   jsonb default null
)
returns table(listing_id bigint)
language sql
stable
as $$
  with
    rules as (
      select
        (r->>'index_name')::text       as index_name,
        (r->>'value')::numeric         as value,
        coalesce(r->>'op', '>=')       as op
      from jsonb_array_elements(coalesce(p_index_rules, '[]'::jsonb)) r
    ),
    prox_rules as (
      select
        (r->>'index_name')::text       as index_name,
        (r->>'value')::numeric         as value,
        coalesce(r->>'op', '>=')       as op
      from jsonb_array_elements(
        coalesce(p_proximity -> 'index_rules', '[]'::jsonb)
      ) r
    )
  select l.listing_id
  from browse_list l
  where
    l.lat is not null and l.lng is not null
    and (
      not (exists (select 1 from rules)
           or p_pop_min is not null
           or p_pop_max is not null)
      or (
        l.home_city_id is not null
        and exists (
          select 1
            from curated_cities_public c
           where c.city_id = l.home_city_id
             and (p_pop_min is null or c.population >= p_pop_min)
             and (p_pop_max is null or c.population <= p_pop_max)
             and not exists (
               select 1 from rules r
               where not exists (
                 select 1 from city_index_values_public v
                 where v.city_id = l.home_city_id
                   and v.index_name = r.index_name
                   and case r.op
                         when '>=' then v.value >= r.value
                         when '<=' then v.value <= r.value
                         when '>'  then v.value >  r.value
                         when '<'  then v.value <  r.value
                         when '==' then v.value =  r.value
                         when '!=' then v.value <> r.value
                         else           v.value >= r.value
                       end
               )
             )
        )
      )
    )
    and (
      p_proximity is null
      or exists (
        select 1
          from curated_cities_public c
         where st_dwithin(
                 st_setsrid(st_makepoint(l.lng, l.lat), 4326)::geography,
                 st_setsrid(st_makepoint(c.lng, c.lat), 4326)::geography,
                 ((p_proximity ->> 'radius_km')::int * 1000)
               )
           and (
             (p_proximity ->> 'population_min')::int is null
             or c.population >= (p_proximity ->> 'population_min')::int
           )
           and not exists (
             select 1 from prox_rules r
             where not exists (
               select 1 from city_index_values_public v
               where v.city_id = c.city_id
                 and v.index_name = r.index_name
                 and case r.op
                       when '>=' then v.value >= r.value
                       when '<=' then v.value <= r.value
                       when '>'  then v.value >  r.value
                       when '<'  then v.value <  r.value
                       when '==' then v.value =  r.value
                       when '!=' then v.value <> r.value
                       else           v.value >= r.value
                     end
             )
           )
      )
    );
$$;

revoke execute on function listings_with_city_quality(jsonb, int, int, jsonb) from public;
grant  execute on function listings_with_city_quality(jsonb, int, int, jsonb) to authenticated, service_role;

-- browse_stats_properties: same home_city_id join for city_index_rules.
-- Every other line reproduced byte-identical from migration 373.
CREATE OR REPLACE FUNCTION public.browse_stats_properties(districts_filter text[] DEFAULT NULL::text[], dispositions_filter text[] DEFAULT NULL::text[], price_min_filter integer DEFAULT NULL::integer, price_max_filter integer DEFAULT NULL::integer, area_min_filter integer DEFAULT NULL::integer, area_max_filter integer DEFAULT NULL::integer, active_only_filter boolean DEFAULT false, last_seen_min_days integer DEFAULT NULL::integer, last_seen_max_days integer DEFAULT NULL::integer, first_seen_min_days integer DEFAULT NULL::integer, first_seen_max_days integer DEFAULT NULL::integer, tom_days_min integer DEFAULT NULL::integer, tom_days_max integer DEFAULT NULL::integer, has_balcony_filter boolean DEFAULT NULL::boolean, has_lift_filter boolean DEFAULT NULL::boolean, has_parking_filter boolean DEFAULT NULL::boolean, inactive_only_filter boolean DEFAULT false, furnished_filter text[] DEFAULT NULL::text[], terrace_filter boolean DEFAULT NULL::boolean, cellar_filter boolean DEFAULT NULL::boolean, garage_filter boolean DEFAULT NULL::boolean, category_sub_cb_filter integer DEFAULT NULL::integer, building_type_filter text[] DEFAULT NULL::text[], tag_ids bigint[] DEFAULT NULL::bigint[], category_main_filter text[] DEFAULT NULL::text[], category_type_filter text DEFAULT NULL::text, bbox_west double precision DEFAULT NULL::double precision, bbox_south double precision DEFAULT NULL::double precision, bbox_east double precision DEFAULT NULL::double precision, bbox_north double precision DEFAULT NULL::double precision, ownership_filter text[] DEFAULT NULL::text[], estate_area_min_filter double precision DEFAULT NULL::double precision, estate_area_max_filter double precision DEFAULT NULL::double precision, usable_area_min_filter double precision DEFAULT NULL::double precision, usable_area_max_filter double precision DEFAULT NULL::double precision, parking_lots_min_filter integer DEFAULT NULL::integer, garden_area_min_filter double precision DEFAULT NULL::double precision, garden_area_max_filter double precision DEFAULT NULL::double precision, condition_match_filter text[] DEFAULT NULL::text[], districts_context_filter text[] DEFAULT NULL::text[], city_index_rules jsonb DEFAULT NULL::jsonb, city_pop_min integer DEFAULT NULL::integer, city_pop_max integer DEFAULT NULL::integer, city_proximity jsonb DEFAULT NULL::jsonb, price_per_m2_min double precision DEFAULT NULL::double precision, price_per_m2_max double precision DEFAULT NULL::double precision, portal_filter text[] DEFAULT NULL::text[], mf_gross_yield_pct_min double precision DEFAULT NULL::double precision, mf_gross_yield_pct_max double precision DEFAULT NULL::double precision, near_pop_5km_min integer DEFAULT NULL::integer, near_pop_15km_min integer DEFAULT NULL::integer, near_jobs_5km_min double precision DEFAULT NULL::double precision, near_jobs_15km_min double precision DEFAULT NULL::double precision, near_youth_5km_min double precision DEFAULT NULL::double precision, near_youth_15km_min double precision DEFAULT NULL::double precision, near_overall_5km_min double precision DEFAULT NULL::double precision, near_overall_15km_min double precision DEFAULT NULL::double precision, districts_excluded_filter boolean[] DEFAULT NULL::boolean[], subtype_filter text[] DEFAULT NULL::text[], recently_added_days integer DEFAULT NULL::integer, recently_changed_days integer DEFAULT NULL::integer, obec_ids_filter bigint[] DEFAULT NULL::bigint[], districts_levels text[] DEFAULT NULL::text[], districts_ids bigint[] DEFAULT NULL::bigint[], building_condition_level_min integer DEFAULT NULL::integer, building_condition_level_max integer DEFAULT NULL::integer, apartment_condition_level_min integer DEFAULT NULL::integer, apartment_condition_level_max integer DEFAULT NULL::integer, price_change_count_min integer DEFAULT NULL::integer, price_change_window_days integer DEFAULT NULL::integer, total_price_change_pct_filter double precision DEFAULT NULL::double precision, with_estimates boolean DEFAULT false, include_no_price boolean DEFAULT false)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
begin
  return (
  with filtered as (
    select l.sreality_id, l.first_seen_at, l.last_seen_at, l.is_active, l.price_czk, l.area_m2, l.disposition, l.tom_days
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
              when lvl = 'locality' then (admin_id is null or l.obec_id = admin_id) and l.place_search_text ilike '%' || needle || '%'
              else (l.district ilike '%' || needle || '%' or l.place_search_text ilike '%' || needle || '%'
                    or l.okres ilike '%' || needle || '%' or l.region ilike '%' || needle || '%')
                and (ctx is null or ctx = '' or l.district ilike '%' || ctx || '%' or l.place_search_text ilike '%' || ctx || '%'
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
              when lvl = 'locality' then (admin_id is null or l.obec_id = admin_id) and l.place_search_text ilike '%' || needle || '%'
              else (l.district ilike '%' || needle || '%' or l.place_search_text ilike '%' || needle || '%'
                    or l.okres ilike '%' || needle || '%' or l.region ilike '%' || needle || '%')
                and (ctx is null or ctx = '' or l.district ilike '%' || ctx || '%' or l.place_search_text ilike '%' || ctx || '%'
                     or l.okres ilike '%' || ctx || '%' or l.region ilike '%' || ctx || '%')
            end
        )
      )
      and (dispositions_filter    is null or l.disposition     = any(dispositions_filter))
      and (price_min_filter       is null or (include_no_price and l.price_czk is null) or l.price_czk >= price_min_filter)
      and (price_max_filter       is null or (include_no_price and l.price_czk is null) or l.price_czk <= price_max_filter)
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
      and ((city_index_rules is null or jsonb_array_length(city_index_rules) = 0)
        or (l.home_city_id is not null
            and not exists (select 1 from jsonb_array_elements(coalesce(city_index_rules, '[]'::jsonb)) r
                where not exists (select 1 from city_index_values_public v where v.city_id = l.home_city_id and v.index_name = r->>'index_name'
                  and case coalesce(r->>'op', '>=')
                        when '>=' then v.value >= (r->>'value')::numeric
                        when '<=' then v.value <= (r->>'value')::numeric
                        when '>'  then v.value >  (r->>'value')::numeric
                        when '<'  then v.value <  (r->>'value')::numeric
                        when '==' then v.value =  (r->>'value')::numeric
                        when '!=' then v.value <> (r->>'value')::numeric
                        else           v.value >= (r->>'value')::numeric
                      end))))
      and (city_proximity is null or (l.lat is not null and l.lng is not null and exists (
            select 1 from curated_cities_public c
            where st_dwithin(st_setsrid(st_makepoint(l.lng, l.lat), 4326)::geography, st_setsrid(st_makepoint(c.lng, c.lat), 4326)::geography, ((city_proximity ->> 'radius_km')::int * 1000))
              and ((city_proximity ->> 'population_min')::int is null or c.population >= (city_proximity ->> 'population_min')::int)
              and not exists (select 1 from jsonb_array_elements(coalesce(city_proximity -> 'index_rules', '[]'::jsonb)) r
                where not exists (select 1 from city_index_values_public v where v.city_id = c.city_id and v.index_name = r->>'index_name'
                  and case coalesce(r->>'op', '>=')
                        when '>=' then v.value >= (r->>'value')::numeric
                        when '<=' then v.value <= (r->>'value')::numeric
                        when '>'  then v.value >  (r->>'value')::numeric
                        when '<'  then v.value <  (r->>'value')::numeric
                        when '==' then v.value =  (r->>'value')::numeric
                        when '!=' then v.value <> (r->>'value')::numeric
                        else           v.value >= (r->>'value')::numeric
                      end)))))
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
