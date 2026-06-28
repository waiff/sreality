-- 254_properties_map_mv.sql
--
-- Robust, read-optimized source for the Browse MAP feed (fetchListingsForMap).
--
-- The map ships up to MAP_CAP (50k) points for client-side MapLibre clustering.
-- Read against the live `properties_public` view that scan is cold-fragile: the
-- `properties` table is heavily churned (the */5 property recompute) and bloated
-- (633 MB of indexes over a 227 MB heap, ~65% all-visible at best), so a
-- full-cohort bitmap heap scan swings from ~170 ms warm to >3 s cold (dirty-page
-- writeback + visibility rechecks on ~18k heap blocks) and trips the anon 3 s
-- statement_timeout. No index fixes it: the planner won't (and can't reliably)
-- do an index-only scan on a table this hot (forcing one measured 7.8 s), and a
-- viewport bbox is only a post-filter (no spatial index combines with it).
--
-- Fix (the rent_map_choropleth precedent): materialize the map's columns into a
-- CLEAN, read-only relation. Because it is refreshed-then-static it stays
-- all-visible and cached, so the SAME bitmap heap scan is robust — measured
-- 200 ms FULLY COLD for the broadest cohort (byt+pronajem nationwide, every page
-- read from disk), vs >3 s on the live table. The win is structural: no
-- dirty-page writeback, no visibility recheck, and ~half the heap blocks (no
-- description / broker text bloat).
--
-- Scope: active, non-merged-away properties WITH coordinates (exactly what the
-- map reads — properties_public already filters status='active', the map adds
-- lat/lng NOT NULL). Columns mirror properties_public's FILTERABLE surface so the
-- existing client applyFilters / applyPrefilters chain is a drop-in (.from swap
-- only) — NOT a new SQL filter surface (no plpgsql, the filters stay client-side).
-- The listings-join DISPLAY columns (description, broker_*, floor, price_unit) are
-- intentionally dropped (the map never reads them; they only bloat the scan); the
-- street fallback collapses to p.street (the group-best street recompute already
-- denormalizes, migration 183).
--
-- Freshness: refreshed by refresh_map_mv.yml (REFRESH MATERIALIZED VIEW
-- CONCURRENTLY, ~every 15 min). The map is thus a few minutes behind the live
-- list — acceptable for a density map; the unique index on property_id is what
-- CONCURRENTLY requires. Adding a new Browse FILTER column means adding it here
-- too (one mechanical column add), same as properties_public — documented in the
-- browse-filter-surfaces contract.

create materialized view if not exists properties_map_mv as
select
  p.id                         as property_id,
  p.repr_listing_id            as sreality_id,
  p.first_seen_at, p.last_seen_at, p.is_active,
  p.category_main, p.category_type,
  p.current_price_czk          as price_czk,
  p.area_m2, p.disposition, p.locality, p.district,
  p.locality_district_id, p.locality_region_id,
  p.lat, p.lng,
  p.has_balcony, p.has_parking, p.has_lift, p.building_type, p.condition,
  p.energy_rating, p.estate_area, p.usable_area, p.garden_area, p.category_sub_cb,
  p.furnished, p.terrace, p.cellar, p.garage, p.parking_lots, p.ownership,
  case when p.is_active
       then greatest(0, floor(extract(epoch from now() - p.first_seen_at) / 86400::numeric)::integer)
       else greatest(0, floor(extract(epoch from p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
  end as tom_days,
  case when p.area_m2 is not null and p.area_m2 > 0::numeric and p.current_price_czk is not null
       then round(p.current_price_czk::numeric / p.area_m2, 2)
       else null::numeric end as price_per_m2,
  p.building_condition_level, p.apartment_condition_level,
  p.source, p.street,
  p.mf_reference_rent_czk, p.mf_gross_yield_pct,
  p.obec, p.okres, p.region,
  p.home_obec_pop, p.near_pop_5km, p.near_pop_15km, p.near_jobs_5km, p.near_jobs_15km,
  p.near_youth_5km, p.near_youth_15km, p.near_overall_5km, p.near_overall_15km,
  p.subtype, p.last_change_at,
  p.obec_id, p.okres_id, p.region_id,
  p.price_change_count, p.price_change_count_30d, p.price_change_count_90d,
  p.price_change_count_365d, p.total_price_change_pct,
  concat_ws(', '::text, p.street, p.locality) as place_search_text,
  p.asset_id
from properties p
where p.status = 'active' and p.lat is not null and p.lng is not null;

-- REFRESH CONCURRENTLY requires a unique index.
create unique index if not exists properties_map_mv_pk on properties_map_mv (property_id);

-- Covering index for the map feed: category eq (+ optional lat/lng bbox) as index
-- conditions, with the selected columns carried so the scan stays off the heap
-- where the planner chooses index-only. On the all-visible matview this is robust.
create index if not exists properties_map_mv_cover on properties_map_mv
  (category_main, category_type, lat, lng)
  include (sreality_id, price_czk, disposition, subtype, area_m2, district,
           last_seen_at, first_seen_at, is_active);

grant select on properties_map_mv to anon, authenticated;
