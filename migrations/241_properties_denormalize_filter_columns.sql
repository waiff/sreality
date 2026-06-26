-- 241_properties_denormalize_filter_columns.sql
--
-- Browse filters on several columns that live ONLY on `listings`, but Browse
-- reads the `properties_public` view (properties p LEFT JOIN listings l). The
-- biggest is the district filter (region_id / okres_id / obec_id). Because those
-- columns are on the JOIN side, a district-filtered Browse query must join
-- properties -> listings and test the filter once per candidate row. Live proof
-- for the "Domy, Praha" preset (834-row cohort): 11,640 nested-loop join probes,
-- 15,852 ms, over the anon 3s statement_timeout -> "Count may be stale" + an
-- empty list. (Distinct from migration 239's functional-lat/lng cause.)
--
-- Fix: denormalise every Browse-FILTERABLE listings column onto `properties`,
-- maintained by recompute_property_stats (it already copies the representative
-- listing's columns onto the parent -- migration 095 pattern; this extends that
-- SET list). The view then filters/sorts on `properties` alone; the join remains
-- only for DISPLAY-only columns (floor, broker, description, price_unit), which
-- are cheap to materialise for the 24 returned rows. This is the foundation for
-- the browse_cohort RPC (filter-first BitmapAnd over these columns).
--
-- Types mirror the listings source columns exactly so the CREATE OR REPLACE VIEW
-- in migration 242 keeps every output column's type unchanged.

ALTER TABLE properties
  ADD COLUMN IF NOT EXISTS region_id                 bigint,
  ADD COLUMN IF NOT EXISTS okres_id                  bigint,
  ADD COLUMN IF NOT EXISTS obec_id                   bigint,
  ADD COLUMN IF NOT EXISTS obec                      text,
  ADD COLUMN IF NOT EXISTS okres                     text,
  ADD COLUMN IF NOT EXISTS region                    text,
  ADD COLUMN IF NOT EXISTS building_condition_level  integer,
  ADD COLUMN IF NOT EXISTS apartment_condition_level integer,
  ADD COLUMN IF NOT EXISTS energy_rating             text,
  ADD COLUMN IF NOT EXISTS source                    text,
  ADD COLUMN IF NOT EXISTS locality_district_id      integer,
  ADD COLUMN IF NOT EXISTS locality_region_id        integer;

-- Indexes for the most common, selective filter: the admin hierarchy. An obec or
-- okres pick is selective enough that the planner filters-first off these; the
-- obec_id index also serves the growth / city-quality prefilters' .in('obec_id').
-- (source / condition / energy indexes are added with the browse_cohort RPC,
-- where the BitmapAnd path actually uses them.)
CREATE INDEX IF NOT EXISTS properties_region_id_idx ON properties (region_id) WHERE region_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS properties_okres_id_idx  ON properties (okres_id)  WHERE okres_id  IS NOT NULL;
CREATE INDEX IF NOT EXISTS properties_obec_id_idx   ON properties (obec_id)   WHERE obec_id   IS NOT NULL;

-- Backfill from the representative listing (same row recompute_property_stats's
-- `repr` CTE selects: properties.repr_listing_id). Idempotent re-run safe; a
-- no-op on a fresh rebuild (recompute populates as data loads). On production
-- this is applied in id-range batches to avoid one long transaction.
UPDATE properties p SET
  region_id                 = l.region_id,
  okres_id                  = l.okres_id,
  obec_id                   = l.obec_id,
  obec                      = l.obec,
  okres                     = l.okres,
  region                    = l.region,
  building_condition_level  = l.building_condition_level,
  apartment_condition_level = l.apartment_condition_level,
  energy_rating             = l.energy_rating,
  source                    = l.source,
  locality_district_id      = l.locality_district_id,
  locality_region_id        = l.locality_region_id
FROM listings l
WHERE l.sreality_id = p.repr_listing_id;

ANALYZE properties;
