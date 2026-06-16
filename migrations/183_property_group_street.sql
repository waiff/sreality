-- 183: group-aware street for the property grain (place_search_text fix).
--
-- WHY: place_search_text (migration 182) — the column every Browse street pick
-- ILIKEs — was concat_ws(street, locality) of the REPRESENTATIVE listing only.
-- A multi-portal property whose repr is a street-less portal listing
-- (idnes/remax before this work) silently degraded to locality-only even when a
-- sibling listing carried the street; concat_ws just drops the NULL. There was
-- no "best non-null street across the group".
--
-- FIX: denormalize a group-best street onto properties.street, populated by
-- recompute_property_stats (best non-null child street, sreality-preferred —
-- the most reliable, structured source). properties_public.street and
-- place_search_text both COALESCE(p.street, l.street): the new group-best wins
-- once the recompute populates it, and the repr listing's street is the
-- fallback until then — so there is NO regression window between this DDL and
-- the next recompute run, and the column self-heals on every daily sweep.
--
-- Additive: one nullable column + a view re-create. No data backfill here — the
-- recompute job owns the column; the COALESCE keeps reads correct meanwhile.

alter table properties add column if not exists street text;

comment on column public.properties.street is
  'Group-best street: the best non-null child street (sreality-preferred), '
  'denormalized by recompute_property_stats so place_search_text matches a '
  'street even when the representative listing lacks one.';

-- properties_public — reproduced VERBATIM from migration 182; the ONLY changes
-- are the two street expressions: l.street -> coalesce(p.street, l.street).
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
    p.max_price_drop_pct, p.stats_computed_at, l.source,
    coalesce(p.street, l.street) AS street,
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
    p.price_change_count_365d, p.total_price_change_pct,
    concat_ws(', ', coalesce(p.street, l.street), p.locality) AS place_search_text
   FROM properties p
     LEFT JOIN listings l ON l.sreality_id = p.repr_listing_id
  WHERE p.status = 'active'::text;

grant select on properties_public to anon;

comment on column public.properties_public.place_search_text is
  'Free-text place words for location-chip matching (group-best street + locality, '', ''-joined). Matching-only — not a display field.';
