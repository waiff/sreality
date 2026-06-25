-- 239_properties_scalar_latlng.sql
--
-- Browse's map-area filter sends scalar lat/lng range predicates
-- (.gte('lat',…).lte('lat',…).gte('lng',…).lte('lng',…)), but
-- properties_public exposed lat/lng as the computed expressions
-- st_y(geom::geometry) / st_x(geom::geometry) — there were no real columns.
-- Postgres keeps NO statistics on a functional expression, so the planner
-- estimated the bbox at rows=1 (actual ~52k for a country-wide box) and
-- therefore abandoned the ordered keyset index, falling back to a full bitmap
-- heap scan of the whole category + top-N sort (+ a 52k-iteration nested-loop
-- join when a listings column is projected) — ~36s, far over the anon 3s
-- statement_timeout. That is the "Query failed: canceling statement due to
-- statement timeout" banner on Browse.
--
-- Fix: materialise lat/lng as REAL columns so ANALYZE builds proper
-- selectivity histograms. With a correct estimate the planner uses the
-- EXISTING keyset indexes (properties_last_seen_keyset_idx, …) and early-stops
-- after one page (~1ms), with the category + bbox applied as inline filters.
-- No new indexes are needed — validated live with EXPLAIN (36 657ms -> ~1ms).
--
-- The columns are kept coherent by a BEFORE INSERT/UPDATE-OF-geom trigger,
-- mirroring migration 140's geom-derivation pattern, so every write path
-- (detail-drain insert, property recompute) stays correct without app changes.
-- A plain ADD COLUMN + trigger + bulk backfill is used (not a STORED generated
-- column) to avoid the table-rewriting ACCESS EXCLUSIVE lock on the live table.

ALTER TABLE properties
  ADD COLUMN IF NOT EXISTS lat double precision,
  ADD COLUMN IF NOT EXISTS lng double precision;

CREATE OR REPLACE FUNCTION properties_set_latlng() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.geom IS NOT NULL THEN
    NEW.lat := ST_Y(NEW.geom::geometry);
    NEW.lng := ST_X(NEW.geom::geometry);
  ELSE
    NEW.lat := NULL;
    NEW.lng := NULL;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS properties_set_latlng_trg ON properties;
CREATE TRIGGER properties_set_latlng_trg
  BEFORE INSERT OR UPDATE OF geom ON properties
  FOR EACH ROW EXECUTE FUNCTION properties_set_latlng();

-- Backfill existing rows. Idempotent (WHERE lat IS NULL) and a no-op on a
-- fresh rebuild (the trigger populates rows as they load). On production this
-- is applied in id-range batches to avoid a single long transaction; the
-- statement below is the canonical definition.
UPDATE properties
   SET lat = ST_Y(geom::geometry), lng = ST_X(geom::geometry)
 WHERE geom IS NOT NULL AND lat IS NULL;

ANALYZE properties;
