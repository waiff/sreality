-- 052_listings_public_tom.sql
-- Promote "time on market" (TOM, called "turned in" by the operator)
-- to a first-class column on listings_public. Same definition as
-- toolkit/velocity._tom_days: still-active listings count from
-- first_seen_at to now() (right-censored, growing); delisted listings
-- count from first_seen_at to last_seen_at (final sojourn). Floor of
-- whole days; never negative.
--
-- Surfacing TOM in the view (rather than recomputing in every browse
-- predicate / card render) means:
--   * browse_stats and the cards / table / map applyFilters chain can
--     filter on `tom_days` directly with the same gte/lte plumbing
--     used by price / area
--   * the listing card's "na trhu X dní" badge reads one column instead
--     of doing date arithmetic in JS
--   * SQL and Python share one definition; the value the velocity
--     toolkit returns matches what the browse panel filters on
--
-- CREATE OR REPLACE preserves the anon SELECT grant from migration 008.
-- Listing column order matches 023 with `tom_days` appended last so any
-- destructuring caller picks it up as an additional key.

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
  case
    when is_active
      then greatest(0, floor(extract(epoch from (now() - first_seen_at)) / 86400)::int)
    else
      greatest(0, floor(extract(epoch from (last_seen_at - first_seen_at)) / 86400)::int)
  end as tom_days
from listings;
