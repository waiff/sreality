-- 439 — W6b: the Browse map stops sampling its own cohort.
--
-- THE DEFECT. `fetchListingsForMap` issues an UNORDERED `.limit(50000)` against
-- properties_map_mv. Measured live 2026-08-26 on the default cohort
-- (category_main='byt', category_type='pronajem', no bbox):
--
--   Limit -> Index Scan using properties_map_mv_cover
--     Buffers: shared hit=799 read=3139   (3,938 blocks)  rows=50,000  22.66 MB
--
-- The matview's only usable index is ordered (category_main, category_type,
-- lat, lng), so "the first 50,000 rows" means "the 50,000 southernmost rows of
-- the cohort". 104,232 properties are mappable; 54,232 of them — 52.0 percent —
-- are never sent, and every one of them lies north of roughly lat 50.025. The
-- worst cohort (dum/prodej) hides 62.8 percent. Five of 27 cohorts exceed the
-- cap. This is a CORRECTNESS defect: the map is not showing a big cohort
-- badly, it is showing a planner-chosen half of it as though it were the
-- whole. Corollary F: a LIMIT without an ORDER BY is a sample, and a sample
-- chosen by the planner is not a contract.
--
-- Measured production traffic is ~9 map loads/day, so this migration is NOT
-- justified on server load. It is justified on correctness and on client
-- payload.
--
-- AND THE CLIENT CAP IS NOT THE ONLY CAP. `authenticator` carries
-- pgrst.db_max_rows=50000 (migration 394, verified live), so deleting the
-- client `.limit()` would not lift the truncation — it would only stop the
-- client knowing about it.
--
-- ---------------------------------------------------------------------------
-- WHAT THIS SHIPS
-- ---------------------------------------------------------------------------
-- browse_map_cells(): one read that returns, for the SAME cohort the map
-- already defines, either
--
--   * an integer-division GRID AGGREGATE — at most GRID_COLS x GRID_ROWS
--     cells, each carrying its count and the mean position of the points in
--     it — when the cohort has more mappable rows than `point_budget`, or
--   * nothing but the total, when it does not, so the caller falls back to
--     today's per-point read, which is already correct at that size.
--
-- Measured live 2026-08-26, same default cohort, same instance:
--
--   HashAggregate
--     -> Index Only Scan using properties_map_mv_cover
--        Heap Fetches: 0
--        Buffers: shared hit=1627      (1,627 blocks)  171 groups
--
-- 1,627 blocks against 3,938, ~8 KB against 22.66 MB, and it counts 104,232
-- of 104,232 rather than 50,000 of them. Prague-bbox equivalent: 619 blocks,
-- 208 cells, 33,113 rows, Heap Fetches still 0.
--
-- The Heap Fetches: 0 is FRAGILE and is the reason this function aggregates
-- the columns it does. The cover index is
--
--   (category_main, category_type, lat, lng)
--   INCLUDE (sreality_id, price_czk, disposition, subtype, area_m2, district,
--            last_seen_at, first_seen_at, is_active)
--
-- so lat / lng / is_active / price_czk are index-resident and free, while
-- obec_id, property_id, listing_id and source are NOT — naming any of them in
-- the aggregate's target list turns the Index Only Scan into an Index Scan and
-- the win evaporates. They stay reachable in the WHERE (a filtered read is
-- allowed to cost more; an unfiltered one must not).
--
-- WHY NOT PostGIS. properties_map_mv has no geometry column — lat/lng are
-- plain double precision — and no spatial index. PostGIS is installed, but any
-- ST_* route needs a per-row ST_MakePoint, which forfeits exactly the
-- index-only scan the whole design rests on. Integer division on the two
-- columns that ARE in the index is the cheap shape here, not the primitive one.
--
-- WHY AGAINST properties_map_mv AND NOT browse_stats_properties. The STOP 1 /
-- F4 re-measurement (roadmap/hydration-sprint.md) set the rule: fold the map
-- into the stats RPC only if F4 cleared browse_list. It did not — browse_list
-- still costs 2,924,421 blocks/call and takes 8 statement-timeout kills a day.
-- So this accepts an INTERIM FOURTH COPY of the cohort predicate (Browse's
-- client-side applyFilters, the watchdog matcher, browse_stats_properties, and
-- this). Consolidating it into browse_stats_properties is FILED, not attempted
-- here. Every predicate line below is a verbatim transcription of migration
-- 436's browse_stats_properties `filtered` CTE with `browse_list l` swapped for
-- `properties_map_mv l`; keep them line-for-line comparable so the copy stays
-- diffable.
--
-- ---------------------------------------------------------------------------
-- THE GRID, AND WHY IT NEEDS NO NEW CLIENT STATE
-- ---------------------------------------------------------------------------
-- The grid EXTENT is coalesce(the cohort's own bbox, the Czech Republic). The
-- map already writes its viewport into the URL (`filters.bounds` ->
-- effectiveBbox -> bbox_west/south/east/north), so panning or zooming already
-- re-scopes the cohort — which means the grid follows the viewport for free,
-- with no zoom parameter, no new react-query key dimension and no plumbing
-- through ListingMap. Zoom in far enough and the cohort drops under
-- `point_budget` and real pins come back.
--
-- The cell is extent_span / GRID_COLS (resp. GRID_ROWS), the origin is the
-- extent's south-west corner, and the north/east edge indices are clamped into
-- the last row/column. So the cell count is <= GRID_COLS * GRID_ROWS BY
-- CONSTRUCTION — there is no LIMIT anywhere in this function, which is the
-- point: a bounded result, not a bounded read of an unbounded result.
--
-- 20 x 13 = 260 comes from the client's 64-px cell rule: a 1280x800 viewport
-- holds 20 x 12.5 cells of 64 px at any zoom, ~11 KB of payload. That is the
-- floor this design is measured against, and the measured national aggregate
-- (171 groups) and Prague aggregate (208 cells) both sit under it.
--
-- ROWS OUTSIDE THE EXTENT ARE COUNTED, NEVER RELOCATED. With no bbox the
-- extent is the CZ box, and 105 of the default cohort's 104,232 rows (0.10 percent)
-- carry coordinates outside it — the data holds lng values from -118 to +125.
-- Folding them into an edge cell would invent a location; dropping them would
-- repeat the defect this migration exists to fix. They aggregate into the
-- single cx IS NULL group instead, are reported as `off_grid`, and are still
-- inside `total`. When a bbox IS set the extent equals it, so off_grid is
-- necessarily 0.
--
-- `total` IS THE MAPPABLE TOTAL, and is deliberately smaller than the cohort
-- total the header shows: browse_list counts properties without coordinates,
-- properties_map_mv does not. For the default cohort that is 106,173 against
-- 104,232. That gap is CORRECT and the pill says "X of Y mapped" because of it.
--
-- ADDITIVE and autonomous under the database gate: one new function, one
-- EXECUTE grant. Nothing is dropped, nothing is rewritten, and no existing
-- caller changes behaviour.
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

-- Reachable by the logged-in SPA only. properties_map_mv itself is granted to
-- `authenticated` and never to `anon` (migration 299 / test_browse_grant_drift),
-- and this function is SECURITY INVOKER, so an anon call would fail on the
-- relation anyway -- but a function's default ACL is EXECUTE to PUBLIC, so
-- leaving it would publish an anon-callable endpoint that merely happens to
-- error. A DO block over pg_proc rather than a transcribed 76-argument
-- signature: transcribing that signature by hand is how a revoke silently
-- targets nothing (migration 428's lesson).
do $$
declare
  r record;
  n int := 0;
begin
  for r in
    select p.oid::regprocedure::text as sig
      from pg_proc p
      join pg_namespace ns on ns.oid = p.pronamespace
     where ns.nspname = 'public'
       and p.proname = 'browse_map_cells'
  loop
    execute format('revoke execute on function %s from public, anon', r.sig);
    execute format('grant execute on function %s to authenticated', r.sig);
    n := n + 1;
  end loop;

  if n <> 1 then
    raise exception '439: expected exactly 1 browse_map_cells overload, found %', n;
  end if;
end
$$;

-- The function is new, so PostgREST's schema cache does not know it yet; without
-- this the first /rpc/browse_map_cells is a 404 until the next unrelated reload.
select pg_notify('pgrst', 'reload schema');
