-- 093_property_public_views.sql
-- Slice 1 of the multi-portal dedup track: the property-grain public read
-- surface the frontend Browse cohort fetchers repoint onto.
--
-- Two anon-readable views, same posture as migration 008's listings_public:
-- plain views owned by the migration role, `grant select to anon`, no
-- secrets, lat/lng projected off the PostGIS geography as plain numbers.
--
--   properties_public      - one row per canonical property. Mirrors every
--                            listings_public column so browse_stats_properties
--                            (094) can reuse the exact same WHERE shape, plus
--                            the six derived-aggregate columns the async
--                            recompute job maintains. Canonical lifecycle /
--                            price / geo / category come from `properties`
--                            (the rollup); the richer filter attributes that
--                            `properties` does not store (locality, floor,
--                            has_*, condition, broker_*, description, ...)
--                            come from the representative listing.
--   property_sources_public - link history: one row per child listing with
--                            its source / url / active-state / price, for the
--                            Listing Detail "listed on N sites" panel.
--
-- `sreality_id` is exposed as the representative listing's id so the existing
-- frontend plumbing (detail links, image / snapshot / tag lookups, the
-- tag and city-quality prefilters that key on sreality_id) keeps working
-- unchanged while Browse becomes one-dot-per-property. Today every property
-- is a singleton, so sreality_id == repr_listing_id == the sole child and
-- the surface is visually identical to listings_public.

create view properties_public as
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
  l.locality,
  p.district,
  l.locality_district_id,
  l.locality_region_id,
  ST_Y(p.geom::geometry)        as lat,
  ST_X(p.geom::geometry)        as lng,
  l.floor,
  l.total_floors,
  l.has_balcony,
  l.has_parking,
  l.has_lift,
  l.building_type,
  l.condition,
  l.energy_rating,
  l.estate_area,
  l.usable_area,
  l.garden_area,
  l.category_sub_cb,
  l.furnished,
  l.terrace,
  l.cellar,
  l.garage,
  l.parking_lots,
  l.ownership,
  l.broker_name,
  l.broker_email,
  l.broker_phone,
  case
    when p.is_active then GREATEST(0, floor(EXTRACT(epoch FROM now() - p.first_seen_at) / 86400::numeric)::integer)
    else GREATEST(0, floor(EXTRACT(epoch FROM p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
  end                           as tom_days,
  case
    when p.area_m2 is not null and p.area_m2 > 0::numeric and p.current_price_czk is not null
      then p.current_price_czk::numeric / p.area_m2::numeric
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
  p.stats_computed_at
from properties p
left join listings l on l.sreality_id = p.repr_listing_id;

create view property_sources_public as
select
  l.property_id,
  l.sreality_id,
  l.source,
  l.source_url,
  l.source_id_native,
  l.is_active,
  l.price_czk,
  l.first_seen_at,
  l.last_seen_at
from listings l
where l.property_id is not null;

grant select on properties_public      to anon;
grant select on property_sources_public to anon;
