-- 154_property_subtype.sql
--
-- Denormalize the portal-agnostic subtype (migration 152) onto the `properties`
-- parent so the property-grain Browse/Watchdog surfaces can filter on it without
-- joining listings. Mirrors the migration 095 denormalization pattern — the
-- column is maintained by scripts/recompute_property_stats.py (the representative
-- child's subtype) and the insert-time singleton-create in scraper/db.py.
--
-- properties_public is reproduced verbatim from migration 141 (current prod def);
-- the ONLY change is `p.subtype` appended as the trailing column (create or
-- replace forbids reordering existing columns).

alter table properties add column if not exists subtype text;

-- Backfill from each property's representative listing.
update properties p set subtype = l.subtype
from listings l
where l.sreality_id = p.repr_listing_id
  and p.subtype is distinct from l.subtype;

create or replace view properties_public as
 SELECT p.id AS property_id, p.repr_listing_id AS sreality_id, p.first_seen_at,
    p.last_seen_at, p.is_active, p.category_main, p.category_type,
    p.current_price_czk AS price_czk, l.price_unit, p.area_m2, p.disposition,
    p.locality, p.district, l.locality_district_id, l.locality_region_id,
    st_y(p.geom::geometry) AS lat, st_x(p.geom::geometry) AS lng,
    l.floor, l.total_floors, p.has_balcony, p.has_parking, p.has_lift,
    p.building_type, p.condition, l.energy_rating, p.estate_area, p.usable_area,
    p.garden_area, p.category_sub_cb, p.furnished, p.terrace, p.cellar, p.garage,
    p.parking_lots, p.ownership, l.broker_name, l.broker_email, l.broker_phone,
        CASE WHEN p.is_active THEN GREATEST(0, floor(EXTRACT(epoch FROM now() - p.first_seen_at) / 86400::numeric)::integer)
             ELSE GREATEST(0, floor(EXTRACT(epoch FROM p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer) END AS tom_days,
        CASE WHEN p.area_m2 IS NOT NULL AND p.area_m2 > 0::numeric AND p.current_price_czk IS NOT NULL THEN p.current_price_czk::numeric / p.area_m2
             ELSE NULL::numeric END AS price_per_m2,
    l.building_condition_level, l.apartment_condition_level, l.description,
    p.source_count, p.distinct_site_count, p.price_drop_count, p.price_rise_count,
    p.max_price_drop_pct, p.stats_computed_at, l.source, l.street,
    l.mf_reference_rent_czk, l.mf_gross_yield_pct,
    l.obec, l.okres, l.region,
    p.home_obec_pop, p.near_pop_5km, p.near_pop_15km, p.near_jobs_5km,
    p.near_jobs_15km, p.near_youth_5km, p.near_youth_15km, p.near_overall_5km,
    p.near_overall_15km,
    p.subtype
   FROM properties p
     LEFT JOIN listings l ON l.sreality_id = p.repr_listing_id
  WHERE p.status = 'active'::text;

grant select on properties_public to anon;
