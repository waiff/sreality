-- 171_listings_okres_region_id.sql
--
-- Complete the normalized admin-id key set on listings: okres_id + region_id,
-- the integer RÚIAN codes for the okres (administrative district) and kraj
-- (region) -- the sibling keys to obec_id (migration 162).
--
-- WHY: location filtering (the Browse / Watchdog "District" chip) matched by
-- fuzzy ILIKE substring across the district/locality/okres/region NAME columns.
-- That is wrong at the obec level, because an obec name collides with its okres
-- name (e.g. obec "Jihlava" vs okres "Jihlava", obec "Havlíčkův Brod" vs okres
-- "Havlíčkův Brod"): picking the OBEC matched `okres ilike '%Jihlava%'` and
-- swept in every other obec in the okres (Větrný Jeníkov, Světlá nad Sázavou…).
-- The fix is to match by the stable admin id at the level the user picked.
-- obec_id already exists; this adds the missing okres_id / region_id so every
-- level can match by id.
--
-- The listings admin-geo trigger (migration 140 / 162) ALREADY does the single
-- st_covers PIP into admin_boundaries and walks ob -> ok -> kr to derive the
-- obec/okres/region NAMES + obec_id. It simply discarded ok.id / kr.id. This
-- migration captures them -- zero extra query cost, and by construction the
-- chip's okres_id/region_id (resolved by the SAME PIP server-side) equals what
-- a listing at that point gets, so id = id matching is exact.
--
--   1. listings.okres_id / region_id (plain bigint, matches obec_id) + indexes
--   2. extend listings_set_admin_geo() to set them from the same parent walk
--   3. surface both on listings_public + properties_public (trailing)
--
-- Existing rows are backfilled out-of-band right after this migration (a cheap
-- set-based integer join from obec_id through admin_boundaries.parent_id, no
-- spatial scan -- batched only to stay under the statement timeout).

set local lock_timeout = '5s';

-- 1. columns + partial indexes ------------------------------------------------
alter table listings
  add column if not exists okres_id  bigint,
  add column if not exists region_id bigint;

comment on column listings.okres_id is
  'Administrative district (okres) RÚIAN code = admin_boundaries.id (level=okres), '
  'derived from geom via the same PIP + parent walk that fills obec/okres/region.';
comment on column listings.region_id is
  'Region (kraj) RÚIAN code = admin_boundaries.id (level=kraj), derived from geom '
  'via the same PIP + parent walk that fills obec/okres/region.';

create index if not exists listings_okres_id_idx
  on listings (okres_id) where okres_id is not null;
create index if not exists listings_region_id_idx
  on listings (region_id) where region_id is not null;


-- 2. trigger: also capture ok.id (okres_id) + kr.id (region_id) ---------------
-- Reproduced from migration 162; the ONLY changes are the two extra captured
-- ids (v_okres_id / v_region_id) and the cheap-path guard also requiring
-- okres_id so a not-yet-backfilled row self-heals on its next re-detail-fetch.
create or replace function public.listings_set_admin_geo()
returns trigger
language plpgsql
as $function$
declare
  v_obec     text;
  v_okres    text;
  v_kraj     text;
  v_obec_id  bigint;
  v_okres_id bigint;
  v_kraj_id  bigint;
begin
  if new.geom is null then
    return new;
  end if;

  -- Cheap path: an unchanged, already-resolved point needs no PIP. Also require
  -- okres_id so a re-detail-fetch of a not-yet-backfilled row self-heals.
  if tg_op = 'UPDATE'
     and new.geom is not distinct from old.geom
     and new.okres is not null
     and new.obec_id is not null
     and new.okres_id is not null then
    return new;
  end if;

  select ob.id, ob.name, ok.id, ok.name, kr.id, kr.name
    into v_obec_id, v_obec, v_okres_id, v_okres, v_kraj_id, v_kraj
  from admin_boundaries ob
  left join admin_boundaries ok on ok.id = ob.parent_id and ok.level = 'okres'
  left join admin_boundaries kr on kr.id = ok.parent_id and kr.level = 'kraj'
  where ob.level = 'obec'
    and st_covers(ob.geom, new.geom)
  limit 1;

  new.obec      := v_obec;
  new.okres     := v_okres;
  new.region    := v_kraj;
  new.obec_id   := v_obec_id;
  new.okres_id  := v_okres_id;
  new.region_id := v_kraj_id;

  -- Display `district`: fill only when missing (preserve sreality labels).
  if new.district is null then
    if v_kraj = 'Hlavní město Praha' then
      new.district := v_obec;
    elsif v_okres is not null then
      new.district := 'okres ' || v_okres;
    end if;
  end if;

  return new;
end;
$function$;


-- 3a. listings_public: expose okres_id + region_id (trailing) -----------------
-- Reproduced VERBATIM from migration 162; the ONLY change is the two trailing
-- columns.
create or replace view listings_public as
 SELECT sreality_id, first_seen_at, last_seen_at, is_active, category_main,
    category_type, price_czk, price_unit, area_m2, disposition, locality,
    district, locality_district_id, locality_region_id,
    st_y(geom::geometry) AS lat, st_x(geom::geometry) AS lng,
    floor, total_floors, has_balcony, has_parking, has_lift, building_type,
    condition, energy_rating, estate_area, usable_area, garden_area,
    category_sub_cb, furnished, terrace, cellar, garage, parking_lots, ownership,
    broker_name, broker_email, broker_phone,
        CASE WHEN is_active THEN GREATEST(0, floor(EXTRACT(epoch FROM now() - first_seen_at) / 86400::numeric)::integer)
             ELSE GREATEST(0, floor(EXTRACT(epoch FROM last_seen_at - first_seen_at) / 86400::numeric)::integer) END AS tom_days,
        CASE WHEN area_m2 IS NOT NULL AND area_m2 > 0::numeric AND price_czk IS NOT NULL THEN price_czk::numeric / area_m2::numeric
             ELSE NULL::numeric END AS price_per_m2,
    building_condition_level, apartment_condition_level, description, source,
    street, house_number, mf_reference_rent_czk, mf_gross_yield_pct,
    mf_reference_rent,
    obec, okres, region,
    subtype,
    obec_id,
    okres_id, region_id
   FROM listings;

grant select on listings_public to anon;


-- 3b. properties_public: expose the repr listing's okres_id + region_id --------
-- Reproduced VERBATIM from migration 162; the ONLY change is the two trailing
-- columns sourced from the repr listing join.
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
    l.okres_id, l.region_id
   FROM properties p
     LEFT JOIN listings l ON l.sreality_id = p.repr_listing_id
  WHERE p.status = 'active'::text;

grant select on properties_public to anon;
