-- 065_listings_public_ppm2.sql
-- Surface price-per-square-metre as a first-class column on
-- listings_public so the Browse cards / table / map sort order can
-- include it without forcing the client to compute over a paginated
-- slice. Identical math to fmtPricePerM2 in frontend/src/lib/format.ts:
-- price_czk / area_m2 when both are present and area_m2 > 0, else NULL.
-- NULL cases drop to the bottom under PostgREST's `nullsFirst: false`.
--
-- Mirrors migration 054_listings_public_tom: CREATE OR REPLACE
-- preserves the anon SELECT grant, and every column from the prior
-- view shape must be re-listed or it silently disappears (along with
-- broker_{name,email,phone} added in 026 and tom_days added in 054).

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
  broker_name, broker_email, broker_phone,
  case
    when is_active
      then greatest(0, floor(extract(epoch from (now() - first_seen_at)) / 86400)::int)
    else
      greatest(0, floor(extract(epoch from (last_seen_at - first_seen_at)) / 86400)::int)
  end as tom_days,
  case
    when area_m2 is not null and area_m2 > 0 and price_czk is not null
      then (price_czk::numeric / area_m2::numeric)
    else null
  end as price_per_m2
from listings;
