-- 026_listings_public_broker.sql
-- Extend listings_public to surface the broker contact columns
-- added by 025_broker_fields. Captured retroactively as a
-- documentation file: the live production DB had this applied
-- (supabase migration version 20260510164202) but the migrations/
-- folder was missing it. Recovered verbatim from
-- supabase_migrations.schema_migrations.
--
-- CREATE OR REPLACE preserves the anon SELECT grant from migration
-- 008. Subsequent CREATE OR REPLACE statements on listings_public
-- (such as 054_listings_public_tom) must re-include these columns
-- or the view silently loses them.

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
  furnished, terrace, cellar, garage, parking_lots, ownership,
  broker_name, broker_email, broker_phone
from listings;
