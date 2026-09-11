-- 495_data_quality_source_url.sql
--
-- One more field on the per-portal completeness census: `source_url`
-- (docs/design/portal-listing-url.md, W3).
--
-- A listing's page URL is a stored fact for all nine portals since W0 (#1400);
-- history was filled by scripts/reconcile_source_url.py. The failure this row
-- watches for is silent by construction: sreality can add a sub-category code
-- at any time, the closed codebook in scraper/sreality_url.py yields NULL for it
-- (never a guess), and nothing else would notice — the SPA simply shows no chip.
-- This VALUES tuple gives the operator-facing per-source trend for free: the
-- existing `capture-data-quality` pg_cron job (migration 179, every 6 h) already
-- snapshots the whole view into data_quality_snapshots, so one line of SQL buys a
-- history. The alarm is scripts/verify_pipeline.py's `outbound_url_coverage`
-- (absolute per-source count of active rows with no URL) — a percentage over
-- ~800k rows would never move for a single new code; a count moves within a day.
--
-- Body copied VERBATIM from migration 318 (the only definition of this view;
-- 427 mentions it in prose only) with the one tuple appended; the column list is
-- unchanged, so `create or replace` is clean and the platform-admin gate stays.
-- Additive, view-only, one short transaction.

begin;
set local lock_timeout = '5s';

create or replace view public.data_quality_by_source as
select * from (SELECT l.source,
    v.field,
    count(*) AS n_active,
    count(*) FILTER (WHERE v.present) AS n_populated,
    round(100.0 * count(*) FILTER (WHERE v.present)::numeric / count(*)::numeric, 1) AS pct_populated
   FROM listings l
     CROSS JOIN LATERAL ( VALUES ('price_czk'::text,l.price_czk IS NOT NULL), ('area_m2'::text,l.area_m2 IS NOT NULL), ('disposition'::text,l.disposition IS NOT NULL), ('category_main'::text,l.category_main IS NOT NULL), ('category_type'::text,l.category_type IS NOT NULL), ('geom'::text,l.geom IS NOT NULL), ('locality'::text,l.locality IS NOT NULL), ('district'::text,l.district IS NOT NULL), ('locality_district_id'::text,l.locality_district_id IS NOT NULL), ('locality_region_id'::text,l.locality_region_id IS NOT NULL), ('street'::text,l.street IS NOT NULL), ('house_number'::text,l.house_number IS NOT NULL), ('floor'::text,l.floor IS NOT NULL), ('total_floors'::text,l.total_floors IS NOT NULL), ('has_balcony'::text,l.has_balcony IS NOT NULL), ('has_lift'::text,l.has_lift IS NOT NULL), ('has_parking'::text,l.has_parking IS NOT NULL), ('terrace'::text,l.terrace IS NOT NULL), ('cellar'::text,l.cellar IS NOT NULL), ('garage'::text,l.garage IS NOT NULL), ('parking_lots'::text,l.parking_lots IS NOT NULL), ('building_type'::text,l.building_type IS NOT NULL), ('condition'::text,l.condition IS NOT NULL), ('energy_rating'::text,l.energy_rating IS NOT NULL), ('furnished'::text,l.furnished IS NOT NULL), ('ownership'::text,l.ownership IS NOT NULL), ('building_condition_level'::text,l.building_condition_level IS NOT NULL), ('apartment_condition_level'::text,l.apartment_condition_level IS NOT NULL), ('property_grouped'::text,l.property_id IS NOT NULL), ('source_url'::text,l.source_url IS NOT NULL)) v(field, present)
  WHERE l.is_active
  GROUP BY l.source, v.field
) __admin_gate
where is_platform_admin();

commit;
