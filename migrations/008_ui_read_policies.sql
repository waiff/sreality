-- 008_ui_read_policies.sql
--
-- Public read-only views for the U1a database-browser UI.
--
-- The UI talks to Supabase directly with the anon key. We expose only
-- the columns the UI needs, and only via these views (never the raw
-- tables). lat/lng are projected from the PostGIS geography column so
-- PostgREST can serialize them as plain numbers; the underlying geom
-- is never sent over the wire.
--
-- Note: this file was applied to the live database on 2026-05-04 via
-- the Supabase MCP before being committed here. Re-running is a no-op
-- because Supabase records each version once; the SQL is preserved
-- as-applied so future fresh rebuilds reproduce the live schema.

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
