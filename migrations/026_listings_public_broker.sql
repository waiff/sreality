-- 026_listings_public_broker.sql
-- Surface broker_name / broker_email / broker_phone (added in 025) on
-- the anon-readable view so the SPA's Listing Detail page can render
-- the agent contact card without going through the FastAPI service.
--
-- The data is already public on every sreality.cz listing page; this
-- view aggregates it for our 47k-row catalog. Acknowledged trade-off
-- (see CLAUDE.md "Frontend territory" section): scrape-cheap, but no
-- new per-listing exposure. If this stance changes, drop the three
-- columns from this view in a follow-up migration and route them
-- through the token-gated FastAPI service instead.
--
-- Pure projection change; the existing anon SELECT grant from
-- migration 008 persists across CREATE OR REPLACE.

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
