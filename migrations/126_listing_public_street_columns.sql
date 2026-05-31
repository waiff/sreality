-- 126_listing_public_street_columns.sql
--
-- Expose the structured street address on the public views the SPA reads.
-- The columns themselves (listings.street / house_number / zip / street_id)
-- are added + drift-reconciled by migration 122; this migration only widens
-- listings_public (adds street, house_number) and properties_public (adds the
-- representative listing's street) so the /dedup review card can show them.
-- Idempotent create-or-replace; re-grants select to anon.

create or replace view listings_public as
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
    house_number
   FROM listings;

grant select on listings_public to anon;


-- properties_public: migration 118 definition + the representative listing's
-- street (trailing). Everything else byte-for-byte from 118.
create or replace view properties_public as
select
  p.id                          as property_id,
  p.repr_listing_id             as sreality_id,
  p.first_seen_at,
  p.last_seen_at,
  p.is_active,
  p.category_main,
  p.category_type,
  p.current_price_czk           as price_czk,
  l.price_unit,
  p.area_m2,
  p.disposition,
  p.locality,
  p.district,
  l.locality_district_id,
  l.locality_region_id,
  ST_Y(p.geom::geometry)        as lat,
  ST_X(p.geom::geometry)        as lng,
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
  case
    when p.is_active then GREATEST(0, floor(EXTRACT(epoch FROM now() - p.first_seen_at) / 86400::numeric)::integer)
    else GREATEST(0, floor(EXTRACT(epoch FROM p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
  end                           as tom_days,
  case
    when p.area_m2 is not null and p.area_m2 > 0::numeric and p.current_price_czk is not null
      then p.current_price_czk::numeric / p.area_m2
    else null::numeric
  end                           as price_per_m2,
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
  l.street
from properties p
  left join listings l on l.sreality_id = p.repr_listing_id
where p.status = 'active'::text;

grant select on properties_public to anon;
