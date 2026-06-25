-- 240_properties_public_real_latlng.sql
--
-- Repoint properties_public.lat/lng at the real columns added in migration 239
-- (was st_y(geom::geometry) / st_x(geom::geometry)). Output column names and
-- types are unchanged (double precision), so this is a transparent
-- CREATE OR REPLACE VIEW: existing anon SELECT grants and column order are
-- preserved, and the deployed frontend's scalar .gte/.lte('lat'/'lng')
-- predicates now hit indexable columns with real statistics. Apply AFTER the
-- 239 backfill so the columns are populated. Everything else is byte-identical
-- to the prior definition.

CREATE OR REPLACE VIEW properties_public AS
 SELECT p.id AS property_id,
    p.repr_listing_id AS sreality_id,
    p.first_seen_at,
    p.last_seen_at,
    p.is_active,
    p.category_main,
    p.category_type,
    p.current_price_czk AS price_czk,
    l.price_unit,
    p.area_m2,
    p.disposition,
    p.locality,
    p.district,
    l.locality_district_id,
    l.locality_region_id,
    p.lat,
    p.lng,
    l.floor,
    l.total_floors,
    p.has_balcony,
    p.has_parking,
    p.has_lift,
    p.building_type,
    p.condition,
    l.energy_rating,
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
        CASE
            WHEN p.is_active THEN GREATEST(0, floor(EXTRACT(epoch FROM now() - p.first_seen_at) / 86400::numeric)::integer)
            ELSE GREATEST(0, floor(EXTRACT(epoch FROM p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
        END AS tom_days,
        CASE
            WHEN p.area_m2 IS NOT NULL AND p.area_m2 > 0::numeric AND p.current_price_czk IS NOT NULL THEN round(p.current_price_czk::numeric / p.area_m2, 2)
            ELSE NULL::numeric
        END AS price_per_m2,
    l.building_condition_level,
    l.apartment_condition_level,
    l.description,
    p.source_count,
    p.distinct_site_count,
    p.price_drop_count,
    p.price_rise_count,
    p.max_price_drop_pct,
    p.stats_computed_at,
    l.source,
    COALESCE(p.street, l.street) AS street,
    p.mf_reference_rent_czk,
    p.mf_gross_yield_pct,
    l.obec,
    l.okres,
    l.region,
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
    l.obec_id,
    l.okres_id,
    l.region_id,
    p.price_change_count,
    p.price_change_count_30d,
    p.price_change_count_90d,
    p.price_change_count_365d,
    p.total_price_change_pct,
    concat_ws(', '::text, COALESCE(p.street, l.street), p.locality) AS place_search_text,
    p.asset_id
   FROM properties p
     LEFT JOIN listings l ON l.sreality_id = p.repr_listing_id
  WHERE p.status = 'active'::text;
