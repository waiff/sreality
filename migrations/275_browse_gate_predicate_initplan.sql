-- 275_browse_gate_predicate_initplan.sql
--
-- Browse read-path fix P0a — stop the publication gate from running per row.
--
-- Migration 273 added the dedup-aware publication gate to properties_public's
-- WHERE as a BARE call:  (NOT publication_gate_enabled() OR published_at IS NOT NULL).
-- `publication_gate_enabled()` is LANGUAGE sql SECURITY DEFINER. A SECURITY DEFINER
-- function CANNOT be inlined by the planner, so the bare call is treated as a
-- volatile-ish qual and evaluated ONCE PER CANDIDATE ROW — ~87k calls for the
-- byt+pronájem cohort. Measured live on properties_public: shared buffers
-- 33.5k -> 172k, warm 146ms -> 914ms, and it times out cold under the anon 3s
-- statement budget. This is what broke the Browse card list + exact-count
-- queries market-wide (the map kept working because it reads the pre-materialized
-- properties_map_mv, where the same predicate runs only at REFRESH time).
--
-- PR #705's commit claimed the reader "evaluates the switch as a one-time
-- InitPlan". That is only true when the call is wrapped in a scalar subquery.
-- Verified with EXPLAIN (ANALYZE) on prod:
--   bare   (NOT publication_gate_enabled() ...)          -> per-row Filter, 172k buffers
--   scalar (NOT (SELECT publication_gate_enabled()) ...) -> "InitPlan 1 ... rows=1 loops=1"
-- i.e. evaluated exactly once and the boolean folded into the qual. Same result,
-- O(1) instead of O(rows). The gate stays OFF-by-default and fully functional.
--
-- This ONLY changes the WHERE. The SELECT list is byte-identical to the live
-- pg_get_viewdef output, so CREATE OR REPLACE VIEW succeeds (no column
-- rename/reorder/drop) and every grant + reloption is preserved. The paired
-- read surface, properties_map_mv, carries the same predicate; its shape is
-- owned by scripts/refresh_map_mv.py (blue-green rebuild), which is wrapped in
-- the same commit — the next scheduled refresh picks it up (readers hit the
-- already-materialized rows, so no anon impact meanwhile).
--
-- Depends on: migration 273 (properties.published_at + publication_gate_enabled()).

CREATE OR REPLACE VIEW properties_public AS
 SELECT p.id AS property_id,
    p.repr_listing_id AS sreality_id,
    p.first_seen_at,
    p.last_seen_at,
    p.is_active,
    p.category_main,
    p.category_type,
    p.current_price_czk AS price_czk,
    l.price_unit,
    p.area_m2,
    p.disposition,
    p.locality,
    p.district,
    p.locality_district_id,
    p.locality_region_id,
    p.lat,
    p.lng,
    l.floor,
    l.total_floors,
    p.has_balcony,
    p.has_parking,
    p.has_lift,
    p.building_type,
    p.condition,
    p.energy_rating,
    p.estate_area,
    p.usable_area,
    p.garden_area,
    p.category_sub_cb,
    p.furnished,
    p.terrace,
    p.cellar,
    p.garage,
    p.parking_lots,
    p.ownership,
    l.broker_name,
    l.broker_email,
    l.broker_phone,
        CASE
            WHEN p.is_active THEN GREATEST(0, floor(EXTRACT(epoch FROM now() - p.first_seen_at) / 86400::numeric)::integer)
            ELSE GREATEST(0, floor(EXTRACT(epoch FROM p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
        END AS tom_days,
        CASE
            WHEN p.area_m2 IS NOT NULL AND p.area_m2 > 0::numeric AND p.current_price_czk IS NOT NULL THEN round(p.current_price_czk::numeric / p.area_m2, 2)
            ELSE NULL::numeric
        END AS price_per_m2,
    p.building_condition_level,
    p.apartment_condition_level,
    l.description,
    p.source_count,
    p.distinct_site_count,
    p.price_drop_count,
    p.price_rise_count,
    p.max_price_drop_pct,
    p.stats_computed_at,
    p.source,
    COALESCE(p.street, l.street) AS street,
    p.mf_reference_rent_czk,
    p.mf_gross_yield_pct,
    p.obec,
    p.okres,
    p.region,
    p.home_obec_pop,
    p.near_pop_5km,
    p.near_pop_15km,
    p.near_jobs_5km,
    p.near_jobs_15km,
    p.near_youth_5km,
    p.near_youth_15km,
    p.near_overall_5km,
    p.near_overall_15km,
    p.subtype,
    p.last_change_at,
    p.obec_id,
    p.okres_id,
    p.region_id,
    p.price_change_count,
    p.price_change_count_30d,
    p.price_change_count_90d,
    p.price_change_count_365d,
    p.total_price_change_pct,
    concat_ws(', '::text, p.street, p.locality) AS place_search_text,
    p.asset_id,
    p.mf_reference_rent,
    p.published_at
   FROM properties p
     LEFT JOIN listings l ON l.sreality_id = p.repr_listing_id
  WHERE p.status = 'active'::text AND (NOT (SELECT publication_gate_enabled()) OR p.published_at IS NOT NULL);


-- Browse read-path fix P0c — the canonical category+recency access-pattern index.
--
-- Every Browse cohort filters by (category_main, category_type) and sorts by a
-- recency column (the default lane is last_seen_at DESC; the "added / newest"
-- presets are first_seen_at DESC). Neither predicate+order was co-indexed: the
-- bare `(last_seen_at, id)` / `(first_seen_at, id)` keyset indexes carry the
-- sort but NOT the category, so a category-filtered lane scanned backward over
-- ALL categories, discarding non-matches. That is catastrophic for last_seen_at
-- specifically: `touch_listings` bumps it for every active listing each scrape
-- cycle, so the newest-by-last_seen rows are whatever portal/category the
-- scraper touched last — measured 4,848 rows discarded before the first 24
-- byt+pronájem matches (1.5s, timing out cold under the anon 3s budget). This is
-- the S1 default-view timeout.
--
-- Fix: composite btrees leading with the category equality, then the recency
-- column DESC + the id tiebreaker DESC (matching keyset.ts's ORDER BY exactly),
-- partial over active rows (the only rows the view returns). The default lane
-- drops to ~19ms / 110 buffers (Index Cond on category, ordered scan, 24 rows,
-- zero wasted scan); the bbox card list (S3) reuses the first_seen composite and
-- lands at ~11ms. Verified live via properties_public as the anon role @3s.
--
-- These SUPPLEMENT (do not replace) the bare keyset + category indexes, which
-- still serve non-category-filtered surfaces (e.g. the watchdog matcher). The
-- trade-off is write cost: the last_seen composite is maintained on every
-- last_seen_at bump, roughly doubling that column's index upkeep — justified by
-- the default lane being every page-load's first query. (A follow-up worth
-- considering: whether last_seen_at is the right DEFAULT sort at all, given it
-- churns market-wide every cycle — see docs/design/browse-read-model.md.)
--
-- On prod these were built CONCURRENTLY out of band (the 446k-row hot table is
-- written every ~5 min, so a plain in-migration build's ACCESS EXCLUSIVE lock
-- would block a scrape write). The statements below are plain + idempotent for a
-- fresh rebuild / replay; a live apply should run them CONCURRENTLY.

CREATE INDEX IF NOT EXISTS properties_cat_last_seen_keyset_idx
  ON properties (category_main, category_type, last_seen_at DESC, id DESC)
  WHERE status = 'active';

CREATE INDEX IF NOT EXISTS properties_cat_first_seen_keyset_idx
  ON properties (category_main, category_type, first_seen_at DESC, id DESC)
  WHERE status = 'active';
