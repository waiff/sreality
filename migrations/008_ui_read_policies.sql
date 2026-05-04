create view listings_public as
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
  building_type, condition, energy_rating
from listings;

create view listing_snapshots_public as
select id, sreality_id, scraped_at, price_czk
from listing_snapshots;

create view listing_freshness_checks_public as
select id, sreality_id, checked_at, outcome
from listing_freshness_checks;

create view listing_fetch_failures_public as
select sreality_id, attempts, first_failure_at, last_failure_at, given_up
from listing_fetch_failures;

grant select on listings_public                 to anon;
grant select on listing_snapshots_public        to anon;
grant select on listing_freshness_checks_public to anon;
grant select on listing_fetch_failures_public   to anon;
