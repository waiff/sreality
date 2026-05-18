-- 073_listings_public_condition_levels.sql
--
-- Extends listings_public with the two condition columns added in
-- migration 072 so the frontend (which connects with the anon key
-- and can only read public views — see CLAUDE.md "Frontend
-- territory") can filter on them.
--
-- Identical column list to the prior listings_public view (defined
-- in migration 066) plus building_condition_level and
-- apartment_condition_level appended at the end. Anon SELECT grant
-- from migration 008 persists across CREATE OR REPLACE VIEW.

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
  st_y(geom::geometry) as lat,
  st_x(geom::geometry) as lng,
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
  case
    when is_active then greatest(0, floor(extract(epoch from now() - first_seen_at) / 86400::numeric)::integer)
    else greatest(0, floor(extract(epoch from last_seen_at - first_seen_at) / 86400::numeric)::integer)
  end as tom_days,
  case
    when area_m2 is not null and area_m2 > 0::numeric and price_czk is not null then price_czk::numeric / area_m2::numeric
    else null::numeric
  end as price_per_m2,
  building_condition_level,
  apartment_condition_level
from listings;
