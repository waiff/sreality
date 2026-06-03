-- 158_property_last_change_at.sql
--
-- "Recently changed" Browse filter support. Adds a precomputed `last_change_at`
-- to the `properties` parent = the most recent `listing_snapshots.scraped_at`
-- across the property's children. Snapshots are append-only and inserted ONLY
-- on a content-hash change (architectural rule #2 — the hash strips the volatile
-- view counter / note fields), so the latest snapshot IS the last time the
-- listing's *meaningful* content (price, area, description, attributes) changed.
--
-- A live `max(scraped_at)` subquery over the 370k+ snapshot history would blow
-- the anon 3s statement timeout on every Browse query, so it's precomputed on
-- `properties` and exposed on properties_public — the same precompute discipline
-- as price_drop_count / the near_* proximity columns. Maintained going forward by
-- scripts/recompute_property_stats.py (dirty-set incremental every 5 min + the
-- daily full sweep; rule #20) alongside the other rollups.
--
-- Pairs with the new "recently added" Browse filter, which reuses the existing
-- first_seen_at column and needs no schema change.

alter table properties add column if not exists last_change_at timestamptz;

-- One-time backfill: the max snapshot time per property. A single set-based
-- statement; the (sreality_id, scraped_at desc) index keeps the per-listing
-- aggregate cheap (~377k snapshot rows). The recompute job keeps it current
-- thereafter. `is distinct from` makes the statement idempotent on re-apply.
update properties p set last_change_at = agg.mx
from (
  select l.property_id as pid, max(s.scraped_at) as mx
  from listing_snapshots s
  join listings l on l.sreality_id = s.sreality_id
  where l.property_id is not null
  group by l.property_id
) agg
where p.id = agg.pid
  and p.last_change_at is distinct from agg.mx;

-- properties_public: reproduced VERBATIM from migration 154 (verified against the
-- live pg_get_viewdef); the ONLY change is `p.last_change_at` appended as the
-- trailing column (create or replace forbids reordering existing columns).
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
    p.subtype,
    p.last_change_at
   FROM properties p
     LEFT JOIN listings l ON l.sreality_id = p.repr_listing_id
  WHERE p.status = 'active'::text;

grant select on properties_public to anon;
