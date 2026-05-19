-- 082_listings_description.sql
--
-- Promote the free-text Czech "Popis" field (raw.text.value) from
-- raw_json to a typed `description` column on `listings`, and expose
-- it via the public read views so the Listing Detail page can render
-- it. Description text is already present in raw_json for every
-- listing we've ever scraped, so the backfill is a one-shot UPDATE
-- from the existing data — no re-scrape needed.
--
-- Snapshots: listing_snapshots stays append-only with raw_json as
-- the canonical per-snapshot record. The public snapshot view
-- projects description directly out of raw_json so the frontend
-- HistoryBlock can flag description changes between snapshots
-- without us materialising another typed column on the history
-- table.

alter table listings add column if not exists description text;

update listings
   set description = raw_json->'text'->>'value'
 where description is null
   and raw_json->'text'->>'value' is not null;

-- Mirrors migration 073's column list (the current shape of the view)
-- with description appended at the end. Anon SELECT grant from
-- migration 008 persists across CREATE OR REPLACE VIEW.
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
  apartment_condition_level,
  description
from listings;

create or replace view listing_snapshots_public as
select
  id,
  sreality_id,
  scraped_at,
  price_czk,
  raw_json->'text'->>'value' as description
from listing_snapshots;

grant select on listings_public          to anon;
grant select on listing_snapshots_public to anon;
