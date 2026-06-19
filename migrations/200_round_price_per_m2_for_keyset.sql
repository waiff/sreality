-- 200_round_price_per_m2_for_keyset.sql
--
-- Round properties_public.price_per_m2 to 2 decimals so the keyset cursor
-- round-trips exactly. price_per_m2 is a computed column (current_price_czk /
-- area_m2) at full numeric precision (~18 fractional digits). When the Table /
-- cards sort by it, the keyset cursor reads that value into a JS Number (≤15-17
-- significant digits), so the boundary value is truncated and the cursor's
-- equal-value tiebreaker (`price_per_m2.eq.<lossy>`) matches NO stored row —
-- silently SKIPPING any rows that share the boundary's exact price/m² at the
-- page seam. Rounding to 2 decimals (Kč/m² to the haléř — ample for sorting
-- AND any display) makes the value fit a float64 exactly, so the eq tiebreaker
-- matches and no row is dropped. Extra rows now sharing a rounded value are
-- handled by the property_id tiebreaker, as designed.
--
-- price_per_m2 is sort-only on this view (cards/table compute the displayed
-- Kč/m² client-side from price + area; browse_stats computes it inline), so the
-- rounding is invisible to every reader. Reproduced verbatim from the live
-- definition with only the price_per_m2 expression changed.

create or replace view properties_public as
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
    st_y(p.geom::geometry) AS lat,
    st_x(p.geom::geometry) AS lng,
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
    concat_ws(', '::text, COALESCE(p.street, l.street), p.locality) AS place_search_text
   FROM properties p
     LEFT JOIN listings l ON l.sreality_id = p.repr_listing_id
  WHERE p.status = 'active'::text;

grant select on properties_public to anon;
