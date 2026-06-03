-- 153_listings_public_subtype.sql
--
-- Expose listings.subtype (migration 152) on listings_public. Reproduced
-- verbatim from migration 141 (current prod def); the ONLY change is `subtype`
-- appended as the trailing column. `create or replace view` forbids reordering
-- existing columns, so it must be added LAST. Browse Map/Table read this view
-- by column name, so position is irrelevant to callers.
--
-- properties_public is updated separately (the subtype column lands on the
-- properties table in the property-grain slice).

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
    subtype
   FROM listings;

grant select on listings_public to anon;
