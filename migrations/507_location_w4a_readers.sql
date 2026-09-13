-- 507_location_w4a_readers.sql
-- W4-a -- every reader takes its location from `listing_location`.
--
-- ADDITIVE. No column is dropped, no data is changed, no view loses a column.
-- Four things happen: one index is built, two serving views change WHERE a
-- column's value comes from (never its name or its type), and the two MF
-- reference-rent functions stop keying on the trigger-filled `ku_id`/`obec_id`
-- and key on the resolver's point instead. W4-c does the dropping.
--
-- APPLY PATH: `apply_migration.yml`, NOT the Supabase MCP. This file contains
-- CREATE INDEX CONCURRENTLY, which cannot run inside a transaction block (25001),
-- and every MCP path wraps its payload in one. The workflow runs
-- `psql -X -q -v ON_ERROR_STOP=1 -f <file>` with deliberately NO
-- --single-transaction, so statements autocommit; CI's schema replay applies
-- each file the same way. There is NO `begin`/`commit` in this file and there
-- must not be -- which also means the timeouts below are plain `SET`, never
-- `SET LOCAL` (outside a transaction SET LOCAL is a silent no-op).
--
-- APPLY BEFORE THE DEPLOY. Every change here is backward-compatible with the
-- bundle running today (same column names, same types), so the safe order is:
-- apply, then merge/deploy.
--
-- ---------------------------------------------------------------------------
-- 1. THE GEOGRAPHY INDEX.
--
-- `listings.geom` is `geography(point,4326)` (001:31); `listing_location.geom`
-- is `geometry(Point,4326)` (501:66). Every spatial reader that moves in this
-- wave therefore changes TYPE, not just table: `ST_DWithin(geography, geography,
-- metres)` silently becomes `ST_DWithin(geometry, geometry, DEGREES)` unless the
-- call site casts `ll.geom::geography` -- which the code half of W4-a does at
-- every one of them. The existing `listing_location_geom_gist` (501:120) is a
-- plain geometry GiST and cannot serve a geography predicate, so the cast would
-- turn a radius search into a seq scan over the corpus. Hence this index, and
-- hence it lands BEFORE the first reader flips.
--
-- ---------------------------------------------------------------------------
-- 2. THE TWO VIEWS.
--
-- `browse_projection` and `listing_feed_public` were re-sourced by migration 503;
-- these two were not. `properties_public` is the WATCHDOG's relation -- its
-- `ST_DWithin` is rebuilt from `lat`/`lng` (api/notifications.py) and
-- `district_where` reads `obec_id/okres_id/region_id` off it -- so until now the
-- watchdog still matched on legacy property geography written by
-- `recompute_property_stats`'s `best_geo` picker. That picker is deleted in this
-- PR; these views are where its last readers go.
--
-- CREATE OR REPLACE, so every column keeps its name, its type and its position
-- (Postgres enforces all three, and `sync_browse_list` inserts POSITIONALLY).
-- Nothing is appended: the census behind this PR found no reader left for the
-- legacy place TEXT on either view except `properties_public.obec` (the kanban
-- board's "Mesto A-Z" sort), which is re-sourced in place from `ll.obec_name` --
-- a new `obec_name` column beside a legacy `obec` would have kept the legacy
-- reader alive, which is the opposite of the wave. `properties_public.district`
-- is untouched: its readers are `region_stats()` / `region_active_by_day()`,
-- which W4-c re-points or drops together with the column.
--
-- Value equality: `admin_boundaries.id` IS the RUIAN code (017:9-12, 083:18,
-- 141:8, 162:25-27, 171:39-44), so `ll.obec_kod` = the old `obec_id` for any row
-- the resolver has placed. A row with no `listing_location` row now reads NULL
-- rather than a trigger-derived code -- the same cutover 503 already made for
-- Browse and the map.
--
-- Between this apply and the next `*/5` rebuild tick, `browse_list` is
-- unaffected (its projection is untouched here).
--
-- ---------------------------------------------------------------------------
-- 3. THE MF RENT-MAP KEY.
--
-- `recompute_mf_gross_yields()` and `recompute_property_mf()` (migration 257)
-- joined the rent map on `listings.ku_id` / `properties.ku_id` -- katastralni
-- uzemi codes written by the admin-geo trigger (289) and by `best_geo`. Neither
-- survives W4, and `listing_location` has no katastr column (there is no address
-- point that carries one either). The replacement is a point-in-polygon against
-- the RUIAN katastr layer, which is already mirrored (`ruian_admin_units.level =
-- 'katastralni_uzemi'`, enum 380:144, loaded by location_data/ruian_boundaries.py)
-- and already walked this way by api/maps.py. It is wrapped in one STABLE
-- function so both callers share it, and it is evaluated AFTER the eligibility
-- predicates, over sale flats only.
--
-- If the katastr layer is not loaded for the current registry version the PIP
-- returns NULL and the calc falls through to its existing obec branch (`vob`) --
-- coarser, never a wrong territory. Verify after applying:
--     select count(*) from ruian_admin_units where level = 'katastralni_uzemi';
--     select count(*) filter (where mf_reference_rent->'territory'->>'level' = 'ku'),
--            count(*) from listings where mf_reference_rent is not null;
--
-- Rollback: re-apply migrations 257 (both functions) and 503/506 (both views);
-- `drop index concurrently listing_location_geog_gist`.

SET lock_timeout = '30s';
SET statement_timeout = '900s';

-- A CONCURRENTLY build that fails leaves an INVALID index behind, which the
-- planner never uses but every write still maintains -- and `if not exists`
-- would happily skip it on the retry. Same guard, same reasoning as 505.
do $$
begin
  if exists (
        select 1
          from pg_class c
          join pg_index i on i.indexrelid = c.oid
          join pg_class t on t.oid = i.indrelid
         where c.relname = 'listing_location_geog_gist'
           and t.relname = 'listing_location'
           and not i.indisvalid) then
    raise notice 'dropping an INVALID leftover from a failed CONCURRENTLY build';
    drop index if exists public.listing_location_geog_gist;
  end if;
end $$;

create index concurrently if not exists listing_location_geog_gist
  on public.listing_location using gist ((geom::geography));

comment on index listing_location_geog_gist is
  'W4-a. Serves ST_DWithin/ST_Distance on ll.geom::geography -- the metre-based '
  'radius every spatial toolkit reader uses. listing_location_geom_gist (501) is '
  'a geometry index and cannot serve those.';

-- ---------------------------------------------------------------------------
-- properties_public -- migration 506's body. Five columns change their SOURCE
-- and nothing else changes: `lat`/`lng` (the Watchdog's ST_DWithin), the three
-- chip codes (district_where), and `obec` (the kanban town sort).
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
    -- KEPT, unlike on browse_projection: `region_stats()` and
    -- `region_active_by_day()` (migrations 425 / 103) still filter
    -- `district = any(districts_filter)` -- a legacy NAME array -- on this view.
    -- Neither has a caller in api/, toolkit/, frontend/src/, scripts/ or the
    -- extension (migration 425 verified that before it re-created region_stats),
    -- but CI's schema-replay lane exercises both against a real database, and
    -- rule 25 deletes a column in the PR that removes its last READER, not
    -- before. W4 re-points or drops the two functions and the column goes with
    -- them.
    p.district,
    p.locality_district_id,
    p.locality_region_id,
    st_y(ll.geom) as lat,
    st_x(ll.geom) as lng,
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
    p.mf_reference_rent_czk,
    p.mf_gross_yield_pct,
    ll.obec_name as obec,
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
    ll.obec_kod  as obec_id,
    ll.okres_kod as okres_id,
    ll.kraj_kod  as region_id,
    p.price_change_count,
    p.price_change_count_30d,
    p.price_change_count_90d,
    p.price_change_count_365d,
    p.total_price_change_pct,
    p.asset_id,
    p.mf_reference_rent,
    p.published_at,
    p.repr_listing_ref_id as listing_id,
    l.source_id_native,
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

revoke all on properties_public from anon, authenticated;
grant select on properties_public to authenticated;

-- ---------------------------------------------------------------------------
-- listings_public -- migration 503's body. `lat`/`lng` and the three chip codes
-- re-sourced; nothing else touched. The legacy place TEXT (`locality`,
-- `district`, `street`, `house_number`, `obec`, `okres`, `region`) stays exactly
-- as it is: those are `listings` columns, and removing a view column needs a
-- DROP + CREATE, which is W4-c's job in the same statement run that drops them.
-- ---------------------------------------------------------------------------

create or replace view listings_public as
select
  sreality_id,
  first_seen_at,
  last_seen_at,
  is_active,
  category_main,
  category_type,
  price_czk,
  price_unit,
  area_m2,
  disposition,
  locality,
  district,
  locality_district_id,
  locality_region_id,
  st_y(ll.geom) as lat,
  st_x(ll.geom) as lng,
  floor,
  total_floors,
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
  broker_name,
  null::text as broker_email,
  null::text as broker_phone,
  case
    when is_active then greatest(0, floor(extract(epoch from now() - first_seen_at) / 86400::numeric)::integer)
    else greatest(0, floor(extract(epoch from last_seen_at - first_seen_at) / 86400::numeric)::integer)
  end as tom_days,
  measure_price_per_m2(price_czk::numeric, area_m2::numeric, category_main, category_type) as price_per_m2,
  building_condition_level,
  apartment_condition_level,
  description,
  source,
  street,
  house_number,
  mf_reference_rent_czk,
  mf_gross_yield_pct,
  mf_reference_rent,
  obec,
  okres,
  region,
  subtype,
  ll.obec_kod  as obec_id,
  ll.okres_kod as okres_id,
  ll.kraj_kod  as region_id,
  id,
  source_id_native,
  property_id,
  measure_price_per_m2_basis(category_main, category_type) as price_per_m2_basis,
  source_url,
  -- ---- appended by migration 503 (W3 S1) ----
  location_display_label(ll.street_name, ll.house_number_cp, ll.house_number_co,
                         ll.obec_name, ll.cast_obce_name, ll.country_code,
                         ll.country_status) as display_label
from listings
     left join listing_location ll on ll.listing_id = listings.id;

revoke all on listings_public from anon;
grant select on listings_public to authenticated;

-- ---------------------------------------------------------------------------
-- ruian_katastr_code(point) -- the katastr key, computed instead of stored.
--
-- The UNION ALL of two single-purpose branches under one LIMIT is copied in
-- shape from api/maps.py's `_CONTAINING_CHAIN_SQL` and
-- location_data/resolver/resolve_db, whose header records why an
-- `IN ('pip','authoritative')` list plans ~800x worse than the union: the
-- subdivided `pip` pieces first (they are what the GiST actually prunes well),
-- the raw authoritative polygon as the partially-loaded-pack fallback. The level
-- filter sits INSIDE each branch on purpose -- outside it, `LIMIT 1` could pick
-- a non-katastr piece and the whole call would return NULL.
--
-- STABLE (not IMMUTABLE): it reads the registry, which a new RUIAN version
-- changes. NULL point -> NULL, no rows scanned.
-- ---------------------------------------------------------------------------

create or replace function public.ruian_katastr_code(p_point geometry)
returns bigint
language sql
stable
parallel safe
as $fn$
  select u.code
  from (
    (select g.unit_id
       from ruian_admin_unit_geometries g
       join ruian_admin_units u2 on u2.id = g.unit_id
      where g.registry_version_id = (select id from registry_versions where is_current limit 1)
        and g.purpose = 'pip'
        and u2.level = 'katastralni_uzemi'
        and st_covers(g.geom, p_point)
      limit 1)
    union all
    (select g.unit_id
       from ruian_admin_unit_geometries g
       join ruian_admin_units u2 on u2.id = g.unit_id
      where g.registry_version_id = (select id from registry_versions where is_current limit 1)
        and g.purpose = 'authoritative'
        and u2.level = 'katastralni_uzemi'
        and st_covers(g.geom, p_point)
      limit 1)
    limit 1
  ) hit
  join ruian_admin_units u on u.id = hit.unit_id
$fn$;

comment on function public.ruian_katastr_code(geometry) is
  'W4-a. Katastralni uzemi RUIAN code containing a point, from the RUIAN mirror '
  'at the current registry version. Replaces listings.ku_id / properties.ku_id '
  '(trigger 289 / best_geo), both dropped in W4-c.';

-- ---------------------------------------------------------------------------
-- The two MF functions -- migration 257's bodies, with the territory sourcing
-- re-keyed and NOTHING else changed (the arithmetic, the adjustment union, the
-- is-distinct-from write guard and the `final` reset arm are verbatim).
--
-- NOTE, unchanged and deliberately so: `recompute_mf_gross_yields()` still
-- writes `where l.sreality_id = f.sreality_id`, so a post-Gate-2 listing with a
-- NULL sreality_id is computed and then matches nothing. Re-keying that UPDATE
-- onto `l.id` would start writing MF rents for eight portals that have never had
-- them -- a Browse-visible behaviour change that does not belong in a wave whose
-- contract is "the same rows match before and after".
-- ---------------------------------------------------------------------------

create or replace function public.recompute_property_mf(p_ids bigint[] default null)
returns integer
language plpgsql
as $function$
declare
  n integer;
begin
  with cand as (
    select
      p.id, p.category_main,
      -- W4-a: the property's place is its representative listing's resolved
      -- point. `properties.geom/ku_id/obec_id` stopped being written in this
      -- wave (recompute_property_stats lost best_geo) and are dropped in W4-c.
      ll.geom as pt, ll.obec_kod as obec_id,
      p.current_price_czk as price_czk, p.area_m2,
      p.has_balcony, p.terrace, p.furnished, p.garage, p.has_lift,
      p.building_type,
      (p.condition = 'novostavba') as is_nov,
      case
        when p.disposition ~ '^[[:space:]]*[01]' then 1
        when p.disposition ~ '^[[:space:]]*2'    then 2
        when p.disposition ~ '^[[:space:]]*3'    then 3
        when p.disposition ~ '^[[:space:]]*[4-9]' then 4
        else null
      end as vk
    from properties p
    left join listing_location ll on ll.listing_id = p.repr_listing_ref_id
    where p.category_type = 'prodej'
      and p.status = 'active'
      and (p_ids is null or p.id = any(p_ids))
  ),
  -- W4-a: the katastr key is no longer a stored column. It is a point-in-polygon
  -- against the RUIAN katastr layer, computed ONCE here -- AFTER the eligibility
  -- predicates that used to sit in `matched`, so the PIP runs over the sale flats
  -- that can match a rent-map cell at all, not over every sale row in the corpus.
  terr as (
    select c.*, public.ruian_katastr_code(c.pt) as ku_id
    from cand c
    where c.category_main = 'byt'
      and c.vk is not null
      and c.price_czk >= 100000
      and c.area_m2 is not null and c.area_m2 >= 12
  ),
  matched as (
    select
      c.id, c.price_czk, c.area_m2, c.vk, c.is_nov,
      c.has_balcony, c.terrace, c.furnished, c.garage, c.has_lift,
      c.building_type,
      coalesce(vku.ruian_code, vob.ruian_code)           as ruian_code,
      coalesce(vku.level, vob.level)                      as level,
      case when vku.ruian_code is not null
           then vku.ku_name else vob.obec_name end        as terr_name,
      coalesce(vku.kraj, vob.kraj)                        as kraj,
      coalesce(vku.source_revision, vob.source_revision)  as source_revision,
      case
        when vku.ruian_code is not null
          then case when c.is_nov then vku.ref_rent_novostavba_per_m2
                    else vku.ref_rent_per_m2 end
        else case when c.is_nov then vob.ref_rent_novostavba_per_m2
                  else vob.ref_rent_per_m2 end
      end                                                 as base
    from terr c
    left join rent_map_values_public vku
      on vku.vk = c.vk and vku.ruian_code = c.ku_id
    left join rent_map_values_public vob
      on vob.vk = c.vk and vob.ruian_code = c.obec_id
    where (vku.ruian_code is not null or vob.ruian_code is not null)
  ),
  adj as (
    select
      m.id,
      coalesce(sum(a.czk_per_m2) filter (where
           (a.attribute = 'balcony'   and m.has_balcony)
        or (a.attribute = 'terrace'   and m.terrace)
        or (a.attribute = 'furnished' and m.furnished = 'ano')
        or (a.attribute = 'garage'    and m.garage)
        or (a.attribute = 'elevator'  and m.has_lift)
        or (a.attribute = 'other_material' and m.is_nov
            and m.building_type is not null
            and m.building_type not in ('panel', 'cihla'))
      ), 0) as adj_sum,
      coalesce(jsonb_agg(
        jsonb_build_object('attribute', a.attribute, 'czk_per_m2', a.czk_per_m2)
        order by a.attribute
      ) filter (where
           (a.attribute = 'balcony'   and m.has_balcony)
        or (a.attribute = 'terrace'   and m.terrace)
        or (a.attribute = 'furnished' and m.furnished = 'ano')
        or (a.attribute = 'garage'    and m.garage)
        or (a.attribute = 'elevator'  and m.has_lift)
        or (a.attribute = 'other_material' and m.is_nov
            and m.building_type is not null
            and m.building_type not in ('panel', 'cihla'))
      ), '[]'::jsonb) as adj_items
    from matched m
    join rent_map_adjustments_public a
      on a.vk = m.vk and a.is_novostavba = m.is_nov
    group by m.id
  ),
  computed as (
    select
      m.id,
      round((m.base + coalesce(a.adj_sum, 0)) * m.area_m2)::integer as rent_czk,
      round((m.base + coalesce(a.adj_sum, 0)) * m.area_m2 * 12
            / m.price_czk * 100, 2) as yield_pct,
      jsonb_build_object(
        'territory', jsonb_build_object(
          'ruian_code', m.ruian_code, 'level', m.level,
          'name', m.terr_name, 'kraj', m.kraj),
        'vk', m.vk,
        'is_novostavba', m.is_nov,
        'source_revision', m.source_revision,
        'base_per_m2', m.base,
        'adjustments', coalesce(a.adj_items, '[]'::jsonb),
        'adjustments_sum_per_m2', coalesce(a.adj_sum, 0),
        'total_per_m2', m.base + coalesce(a.adj_sum, 0),
        'area_m2', m.area_m2,
        'monthly_rent_czk',
          round((m.base + coalesce(a.adj_sum, 0)) * m.area_m2)::integer
      ) as detail
    from matched m
    left join adj a on a.id = m.id
    where m.base is not null
  ),
  final as (
    select c.id, comp.rent_czk, comp.yield_pct, comp.detail
    from cand c
    left join computed comp on comp.id = c.id
  )
  update properties p
    set mf_reference_rent_czk = f.rent_czk,
        mf_gross_yield_pct    = f.yield_pct,
        mf_reference_rent     = f.detail
  from final f
  where p.id = f.id
    and (p.mf_reference_rent_czk is distinct from f.rent_czk
         or p.mf_gross_yield_pct is distinct from f.yield_pct
         or p.mf_reference_rent is distinct from f.detail);

  get diagnostics n = row_count;
  return n;
end;
$function$;

create or replace function public.recompute_mf_gross_yields()
returns integer
language plpgsql
as $function$
declare
  n integer;
begin
  with cand as (
    select
      l.sreality_id, l.category_main,
      -- W4-a: territory from `listing_location`, not from the trigger-filled
      -- `listings.ku_id` / `obec_id` (both dropped in W4-c).
      ll.geom as pt, ll.obec_kod as obec_id, l.price_czk, l.area_m2,
      l.has_balcony, l.terrace, l.furnished, l.garage, l.has_lift,
      l.building_type,
      (l.condition = 'novostavba') as is_nov,
      case
        when l.disposition ~ '^[[:space:]]*[01]' then 1
        when l.disposition ~ '^[[:space:]]*2'    then 2
        when l.disposition ~ '^[[:space:]]*3'    then 3
        when l.disposition ~ '^[[:space:]]*[4-9]' then 4
        else null
      end as vk
    from listings l
    left join listing_location ll on ll.listing_id = l.id
    where l.category_type = 'prodej'
  ),
  -- W4-a: the katastr key is no longer a stored column. It is a point-in-polygon
  -- against the RUIAN katastr layer, computed ONCE here -- AFTER the eligibility
  -- predicates that used to sit in `matched`, so the PIP runs over the sale flats
  -- that can match a rent-map cell at all, not over every sale row in the corpus.
  terr as (
    select c.*, public.ruian_katastr_code(c.pt) as ku_id
    from cand c
    where c.category_main = 'byt'
      and c.vk is not null
      and c.price_czk >= 100000
      and c.area_m2 is not null and c.area_m2 >= 12
  ),
  matched as (
    select
      c.sreality_id, c.price_czk, c.area_m2, c.vk, c.is_nov,
      c.has_balcony, c.terrace, c.furnished, c.garage, c.has_lift,
      c.building_type,
      coalesce(vku.ruian_code, vob.ruian_code)           as ruian_code,
      coalesce(vku.level, vob.level)                      as level,
      case when vku.ruian_code is not null
           then vku.ku_name else vob.obec_name end        as terr_name,
      coalesce(vku.kraj, vob.kraj)                        as kraj,
      coalesce(vku.source_revision, vob.source_revision)  as source_revision,
      case
        when vku.ruian_code is not null
          then case when c.is_nov then vku.ref_rent_novostavba_per_m2
                    else vku.ref_rent_per_m2 end
        else case when c.is_nov then vob.ref_rent_novostavba_per_m2
                  else vob.ref_rent_per_m2 end
      end                                                 as base
    from terr c
    left join rent_map_values_public vku
      on vku.vk = c.vk and vku.ruian_code = c.ku_id
    left join rent_map_values_public vob
      on vob.vk = c.vk and vob.ruian_code = c.obec_id
    where (vku.ruian_code is not null or vob.ruian_code is not null)
  ),
  adj as (
    select
      m.sreality_id,
      coalesce(sum(a.czk_per_m2) filter (where
           (a.attribute = 'balcony'   and m.has_balcony)
        or (a.attribute = 'terrace'   and m.terrace)
        or (a.attribute = 'furnished' and m.furnished = 'ano')
        or (a.attribute = 'garage'    and m.garage)
        or (a.attribute = 'elevator'  and m.has_lift)
        or (a.attribute = 'other_material' and m.is_nov
            and m.building_type is not null
            and m.building_type not in ('panel', 'cihla'))
      ), 0) as adj_sum,
      coalesce(jsonb_agg(
        jsonb_build_object('attribute', a.attribute, 'czk_per_m2', a.czk_per_m2)
        order by a.attribute
      ) filter (where
           (a.attribute = 'balcony'   and m.has_balcony)
        or (a.attribute = 'terrace'   and m.terrace)
        or (a.attribute = 'furnished' and m.furnished = 'ano')
        or (a.attribute = 'garage'    and m.garage)
        or (a.attribute = 'elevator'  and m.has_lift)
        or (a.attribute = 'other_material' and m.is_nov
            and m.building_type is not null
            and m.building_type not in ('panel', 'cihla'))
      ), '[]'::jsonb) as adj_items
    from matched m
    join rent_map_adjustments_public a
      on a.vk = m.vk and a.is_novostavba = m.is_nov
    group by m.sreality_id
  ),
  computed as (
    select
      m.sreality_id,
      round((m.base + coalesce(a.adj_sum, 0)) * m.area_m2)::integer as rent_czk,
      round((m.base + coalesce(a.adj_sum, 0)) * m.area_m2 * 12
            / m.price_czk * 100, 2) as yield_pct,
      jsonb_build_object(
        'territory', jsonb_build_object(
          'ruian_code', m.ruian_code, 'level', m.level,
          'name', m.terr_name, 'kraj', m.kraj),
        'vk', m.vk,
        'is_novostavba', m.is_nov,
        'source_revision', m.source_revision,
        'base_per_m2', m.base,
        'adjustments', coalesce(a.adj_items, '[]'::jsonb),
        'adjustments_sum_per_m2', coalesce(a.adj_sum, 0),
        'total_per_m2', m.base + coalesce(a.adj_sum, 0),
        'area_m2', m.area_m2,
        'monthly_rent_czk',
          round((m.base + coalesce(a.adj_sum, 0)) * m.area_m2)::integer
      ) as detail
    from matched m
    left join adj a on a.sreality_id = m.sreality_id
    where m.base is not null
  ),
  final as (
    select c.sreality_id, comp.rent_czk, comp.yield_pct, comp.detail
    from cand c
    left join computed comp on comp.sreality_id = c.sreality_id
  )
  update listings l
    set mf_reference_rent_czk = f.rent_czk,
        mf_gross_yield_pct    = f.yield_pct,
        mf_reference_rent     = f.detail
  from final f
  where l.sreality_id = f.sreality_id
    and (l.mf_reference_rent_czk is distinct from f.rent_czk
         or l.mf_gross_yield_pct is distinct from f.yield_pct
         or l.mf_reference_rent is distinct from f.detail);

  get diagnostics n = row_count;

  -- properties.mf_* is now computed from the golden record, NOT mirrored from the
  -- representative child's per-listing mf_*.
  perform public.recompute_property_mf(null);

  return n;
end;
$function$;
-- ---------------------------------------------------------------------------
-- The assertion. A re-source is invisible to `\d` -- the column keeps its name,
-- its type and its position -- so the only proof it landed is the view's own
-- definition text. Checked both ways: the new source is present AND the legacy
-- one is gone, because a half-applied re-source would leave the watchdog
-- matching on a column nothing writes any more.
-- ---------------------------------------------------------------------------

do $$
declare
  pp text := pg_get_viewdef('public.properties_public'::regclass);
  lp text := pg_get_viewdef('public.listings_public'::regclass);
  missing text[] := '{}';
begin
  if position('ll.geom' in pp) = 0 then
    missing := missing || 'properties_public.lat/lng not from ll.geom';
  end if;
  if position('ll.obec_kod' in pp) = 0
     or position('ll.okres_kod' in pp) = 0
     or position('ll.kraj_kod' in pp) = 0 then
    missing := missing || 'properties_public chip codes not from listing_location';
  end if;
  if position('ll.obec_name as obec' in lower(pp)) = 0 then
    missing := missing || 'properties_public.obec not from ll.obec_name';
  end if;
  if position('p.lat' in pp) > 0 or position('p.lng' in pp) > 0
     or position('p.obec_id' in pp) > 0 then
    missing := missing || 'properties_public still reads the legacy property geography';
  end if;
  if position('ll.geom' in lp) = 0 then
    missing := missing || 'listings_public.lat/lng not from ll.geom';
  end if;
  if position('listings.geom' in lp) > 0 then
    missing := missing || 'listings_public still reads listings.geom';
  end if;
  if position('ll.obec_kod' in lp) = 0
     or position('ll.okres_kod' in lp) = 0
     or position('ll.kraj_kod' in lp) = 0 then
    missing := missing || 'listings_public chip codes not from listing_location';
  end if;
  if to_regprocedure('public.ruian_katastr_code(geometry)') is null then
    missing := missing || 'ruian_katastr_code(geometry) missing';
  end if;
  if not exists (
        select 1 from pg_class c join pg_index i on i.indexrelid = c.oid
         where c.relname = 'listing_location_geog_gist' and i.indisvalid) then
    missing := missing || 'listing_location_geog_gist missing or INVALID';
  end if;
  if array_length(missing, 1) is not null then
    raise exception 'W4-a did not land: %', array_to_string(missing, '; ');
  end if;
end $$;
