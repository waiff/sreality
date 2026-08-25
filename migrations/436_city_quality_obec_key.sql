-- 436_city_quality_obec_key.sql
-- Cardinality Doctrine W5 (item 2) — city-quality moves onto the obec key.
--
-- THE DEFECT, and the commissioned framing was wrong about it. The city side is 206 rows
-- joined to 6,798. Evaluated ONCE it costs 126 blocks (measured warm, 2.5 ms, 46 cities).
-- The entire 1,778,259-block failure is that it is evaluated once per candidate row —
-- a correlated SubPlan at 282,214 loops, 4.4 blocks each. Nothing was missing except a
-- shape the planner can execute once. That is ~14,100x, not the ~683x the design projected
-- (its "880 blocks once" figure was 7x pessimistic).
--
-- AND A SECOND, INDEPENDENT HARD FAILURE that a SQL fix alone would not touch:
-- resolveCityQualityPrefilter calls fetchAllRows(..., expectMax: 100_000) and the allowlist
-- is 84k-262k listing ids for every practical threshold, so it throws FetchAllOverflowError
-- regardless of SQL speed. Fixing only the plan trades a timeout toast for an overflow toast.
--
-- THIS IS A KEY CHANGE, NOT A RE-EXPRESSION. Recorded plainly because the design's own
-- language obscures it: the live predicate never touches admin_boundary_id. It keys
-- curated_cities.id against browse_list.home_city_id. admin_boundary_id is used only
-- OFFLINE, by recompute_home_city(), to build the ST_Covers anchor set. So "one SQL function
-- owning the same rule evaluation" is true of the RULES and false of the MEMBERSHIP TEST.
--
-- MEMBERSHIP DELTA, re-measured (the design's +1,979 / -49 does not reproduce):
--     +1,960 rows enter, of which 1,916 (97.8%) have NULL lat/lng
--        +44 rows the two keys genuinely disagree on
--        -30 rows drop to an adjacent NON-curated obec
--         19 rows re-attributed between two curated cities
-- The "-49 stale home_city_id corrections" characterisation is UNSUPPORTED. The loss pattern
-- is curated town vs adjacent small obec (Znojmo -> Novy Saldorf-Sedlesovice x5, Ceske
-- Budejovice -> Plana x3, Usti -> Trmice x3); all 43 distinct obec ids are level='obec'.
-- Two geometry-derived columns disagreeing is a DATA DEFECT worth its own look, and it is
-- filed as one — not used to justify this change.
--
-- WHY THE 1,916 COORDINATE-LESS ROWS ARE ADMITTED. listings_with_city_quality opens with
-- `l.lat is not null and l.lng is not null` — a guard that exists only because the SAME
-- function also served the ST_DWithin proximity branch, which this migration RETIRES.
-- A listing in Brno with no coordinates is still in Brno. Keeping the guard would
-- deliberately preserve a silent under-render of the cohort (Corollary F) for the sake of a
-- feature being deleted in the same statement.
--
-- RULE 17 INVERTS, AND THE REPAIR IS IN PYTHON, NOT HERE. Until now the SCHEMA enforced
-- rule 17: `listings` has no home_city_id, so a city-quality clause on a listings-grain
-- query died at parse with 42703 before a row was read. After this migration the same
-- bypass renders `l.obec_id`, which listings HAS — it would plan, execute, and silently
-- return an estimate narrowed by operator-curated, revision-versioned, subjective city
-- scores, with a status='success' row and a full trace. toolkit/comparables.py's
-- _assert_no_city_quality IS that former schema rail. See rule 17 and W5's rails.
--
-- Rollback: migrations/reverts/436_revert_city_quality_obec_key.sql (shipped unapplied).

-- ---------------------------------------------------------------------------
-- 1. The load-bearing invariant, PROVEN not inferred.
--    Assert the RELATIONS, not the literal 206: a legitimately added 207th city must not
--    fail this migration, an UNLINKED one must.
-- ---------------------------------------------------------------------------
do $$
declare
  v_total int; v_linked int; v_obec int; v_dangling int; v_distinct int;
begin
  select count(*) into v_total  from curated_cities;
  select count(*) into v_linked from curated_cities where admin_boundary_id is not null;
  select count(*) into v_obec
    from curated_cities c join admin_boundaries b on b.id = c.admin_boundary_id
   where b.level = 'obec';
  select count(*) into v_dangling
    from curated_cities c
   where c.admin_boundary_id is not null
     and not exists (select 1 from admin_boundaries b where b.id = c.admin_boundary_id);
  select count(distinct admin_boundary_id) into v_distinct from curated_cities;

  if v_total <> v_linked or v_total <> v_obec or v_dangling <> 0 or v_total <> v_distinct then
    raise exception
      'curated_cities obec-key invariant violated: total=% linked=% level_obec=% dangling=% distinct=% '
      '- every curated city must resolve to exactly one distinct level=obec admin_boundaries row '
      '(scripts/ingest_boundaries.relink_curated_cities repairs this).',
      v_total, v_linked, v_obec, v_dangling, v_distinct;
  end if;
  raise notice 'curated_cities obec-key invariant OK: % cities / % distinct obec ids', v_total, v_distinct;
end $$;

-- ---------------------------------------------------------------------------
-- 2. Latest-revision index.
--    NOT CONCURRENTLY, and safe: city_index_values is 13,563 rows with TWO writes in the
--    table's entire history (scripts/seed_curated_cities.py only) and no hot writer -- the
--    opposite of the properties ALTER that lost ~50 lock races. CONCURRENTLY is unavailable
--    through the MCP anyway (SQLSTATE 25001, transaction-wrapped payload).
--
--    The pkey is (city_id, source_revision, index_name), so index_name is NOT a usable
--    prefix after city_id -- measured 3.6 blocks/loop on the pkey for the anti-join below.
-- ---------------------------------------------------------------------------
create index if not exists city_index_values_latest_idx
  on city_index_values (city_id, index_name, source_revision desc);

-- ---------------------------------------------------------------------------
-- 3. LATENT CORRECTNESS BUG, fixed here because the same code is being touched.
--    city_index_values_public filtered `source_revision = (select max(source_revision)
--    from city_index_values)` -- a GLOBAL max with no correlation to city_id or index_name.
--    A partial revision upload (one corrected city at revision 3) would make the view return
--    33 rows instead of 6,798 -- a 99.5% collapse -- and EVERY other city would silently fail
--    EVERY rule, with no error anywhere. Rule 17's "latest revision wins", correctly spelled
--    for a normalized time series, is latest-per-(city_id, index_name).
--
--    Behaviour today is byte-identical (every pair's max IS the global max; both historical
--    uploads were full re-uploads). It diverges only once a partial revision exists, which is
--    the entire point. It also removes a 115-block seq scan per view evaluation.
--
--    NOT EXISTS, not DISTINCT ON: DISTINCT ON marks every non-key output column unsafe for
--    qual pushdown, so `value >= 6` could not be pushed below the Unique node -- and if it
--    ever were, an older revision passing the qual would win. The anti-join has neither problem.
-- ---------------------------------------------------------------------------
create or replace view city_index_values_public as
  select v.city_id, v.index_name, v.value, v.source_revision
    from city_index_values v
   where not exists (
     select 1 from city_index_values newer
      where newer.city_id        = v.city_id
        and newer.index_name     = v.index_name
        and newer.source_revision > v.source_revision
   );

-- ---------------------------------------------------------------------------
-- 4. The one function that owns rule evaluation.
--
--    IT MUST READ THE _public VIEWS, NOT THE BASE TABLES. This is the single most dangerous
--    detail in the migration: curated_cities and city_index_values are both RLS-on with ZERO
--    policies and `authenticated=r`. A SECURITY INVOKER function reading the BASE tables
--    returns ZERO ROWS for the SPA's role -- silently, not as an error -- collapsing every
--    city-quality cohort to empty. The _public views are postgres-owned with security_invoker
--    OFF, so they read across that RLS.
--
--    `parallel safe` matters: one parallel-unsafe function anywhere in a query disables
--    parallelism for the WHOLE plan.
-- ---------------------------------------------------------------------------
create or replace function public.curated_cities_matching(p_index_rules jsonb)
returns setof bigint
language sql
stable
parallel safe
rows 60
as $function$
  select c.admin_boundary_id
    from curated_cities_public c
   where c.admin_boundary_id is not null
     and not exists (
       select 1
         from jsonb_array_elements(coalesce(p_index_rules, '[]'::jsonb)) r
        where not exists (
          select 1
            from city_index_values_public v
           where v.city_id    = c.city_id
             and v.index_name = r->>'index_name'
             and case coalesce(r->>'op', '>=')
                   when '>=' then v.value >= (r->>'value')::numeric
                   when '<=' then v.value <= (r->>'value')::numeric
                   when '>'  then v.value >  (r->>'value')::numeric
                   when '<'  then v.value <  (r->>'value')::numeric
                   when '==' then v.value =  (r->>'value')::numeric
                   when '!=' then v.value <> (r->>'value')::numeric
                   else           v.value >= (r->>'value')::numeric
                 end
        )
     );
$function$;

-- Grant parity with listings_with_city_quality ({postgres, authenticated, service_role},
-- deliberately no anon).
revoke all on function public.curated_cities_matching(jsonb) from public;
grant execute on function public.curated_cities_matching(jsonb) to authenticated, service_role;

comment on function public.curated_cities_matching(jsonb) is
  'W5 (migration 436): the ONE owner of city-quality rule evaluation. Returns the obec ids '
  '(curated_cities.admin_boundary_id) of every curated city whose indexes satisfy every rule. '
  'All three consumers -- Browse, browse_stats_properties and the Watchdog matcher -- reduce '
  'to obec_id = ANY(...), a form with nothing left to diverge on (rule 16). Reads the _public '
  'views deliberately: the base tables are RLS-on with zero policies and would return zero '
  'rows for authenticated, silently emptying every cohort.';

-- ---------------------------------------------------------------------------
-- 5. browse_stats_properties: two hunks, nothing else.
--    The signature is migration 425's byte-for-byte (74 arguments, verified against live:
--    normalised body md5 90c99b58e6a23738174a6eef2c430560 on both). A CREATE OR REPLACE
--    cannot change an argument list, and a typo would create a SECOND overload that
--    PostgREST resolves non-deterministically.
-- ---------------------------------------------------------------------------
--
-- Hunk 1 replaces the 13-line nested-EXISTS branch with ONE predicate.
--   `= any(array(select ...))` is the MEASURED spelling: it forces an InitPlan evaluated
--   once (loops=1) rather than risking the per-row correlated SubPlan that cost 1,778,259
--   blocks. Do NOT respell it `in (select ...)` -- that is the degradation migration 374's
--   own header warned about.
--   The city_index_rules PARAMETER stays alive rather than raising, so a stale SPA bundle
--   still sending it gets the SAME answer as a fresh bundle sending obec_ids_filter --
--   two spellings, one definition, rule 16 intact. W7 drops the parameter.
--
-- Hunk 2 retires city_proximity. `and true` keeps the WHERE list shape; the loud rejection
--   is at the top of the body, because silently ignoring a retired filter WIDENS the cohort.
--
-- Verified: the body this produces is byte-identical to what is deployed
-- (normalised md5 969db06aadc6c989caa8440cd62cfc65 on both).
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