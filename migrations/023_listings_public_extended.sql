-- 023_listings_public_extended.sql
-- Extend the listings_public view with the ten new columns added in 022
-- so the SPA's anon key can read them. Pure projection change; the
-- existing grant on `anon` from migration 008 persists across CREATE
-- OR REPLACE.
--
-- Browser code that already destructures the view continues to work —
-- new fields surface as additional keys, never collide with existing
-- ones.

create or replace view listings_public as
select
  sreality_id, first_seen_at, last_seen_at, is_active,
  category_main, category_type,
  price_czk, price_unit,
  area_m2, disposition,
  locality, district, locality_district_id, locality_region_id,
  ST_Y(geom::geometry) as lat,
  ST_X(geom::geometry) as lng,
  floor, total_floors,
  has_balcony, has_parking, has_lift,
  building_type, condition, energy_rating,
  estate_area, usable_area, garden_area,
  category_sub_cb,
  furnished, terrace, cellar, garage, parking_lots, ownership
from listings;
