-- 334_expose_listing_id_on_public_views.sql
-- R2 Phase C read cutover, step 1 (additive): expose the surrogate listings.id
-- on the public views the frontend resolver chain, ListingDetail, and brokers.ts
-- read, per the runbook §4 MUST-precede-flip list. This is purely additive — a
-- new trailing column on each view, no existing column renamed/reordered/dropped,
-- so CREATE OR REPLACE VIEW succeeds and every grant is preserved untouched
-- (verified live: none of these 4 views carry security_invoker, matching the
-- deliberate shared-market-read pattern the public-release program documented
-- for listings/properties/images).
--
-- Frontend rewiring to actually READ these new columns (queries.ts, ListingDetail.tsx,
-- brokers.ts, api.ts) is a SEPARATE follow-up PR — this migration only makes the
-- data available; nothing downstream depends on it yet, so it's zero-risk to ship
-- alone.

CREATE OR REPLACE VIEW listings_public AS
 SELECT sreality_id,
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
    st_y(geom::geometry) AS lat,
    st_x(geom::geometry) AS lng,
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
    broker_email,
    broker_phone,
        CASE
            WHEN is_active THEN GREATEST(0, floor(EXTRACT(epoch FROM now() - first_seen_at) / 86400::numeric)::integer)
            ELSE GREATEST(0, floor(EXTRACT(epoch FROM last_seen_at - first_seen_at) / 86400::numeric)::integer)
        END AS tom_days,
        CASE
            WHEN area_m2 IS NOT NULL AND area_m2 > 0::numeric AND price_czk IS NOT NULL THEN price_czk::numeric / area_m2::numeric
            ELSE NULL::numeric
        END AS price_per_m2,
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
    obec_id,
    okres_id,
    region_id,
    id
   FROM listings;

CREATE OR REPLACE VIEW property_sources_public AS
 SELECT property_id,
    sreality_id,
    source,
    source_url,
    source_id_native,
    is_active,
    price_czk,
    first_seen_at,
    last_seen_at,
    id
   FROM listings l
  WHERE property_id IS NOT NULL;

CREATE OR REPLACE VIEW listing_natural_key_public AS
 SELECT sreality_id,
    source,
    source_id_native,
    id
   FROM listings;

CREATE OR REPLACE VIEW listing_snapshots_public AS
 SELECT id,
    sreality_id,
    scraped_at,
    price_czk,
    (raw_json -> 'text'::text) ->> 'value'::text AS description,
    listing_id
   FROM listing_snapshots;
