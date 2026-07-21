-- 344_maintenance_walker_indexes_on_id.sql
-- R2 read cutover: the five `listings` partial indexes that the enrichment
-- walkers page through, rebuilt on the surrogate `id`.
--
-- Post-Gate-2 a new non-sreality listing has sreality_id NULL, so `sreality_id
-- > cursor` never matches and the row is INVISIBLE to every one of these
-- walkers: geocoding, street resolution and geo_cell keying silently stop for
-- exactly the new rows, forever, with no error. That in turn starves dedup — no
-- street and no geo_cell means no blocking key, so the listing never reaches a
-- dedup pass at all.
--
-- Applied live via scripts/apply_r2_maintenance_indexes.py (CONCURRENTLY, then
-- the legacy twins dropped only once every replacement is confirmed valid);
-- this is the plain form for fresh rebuilds, same convention as migration 333.
-- Predicates are copied verbatim from the live definitions — a drifted
-- predicate would silently change which rows the walker sees.

CREATE INDEX IF NOT EXISTS listings_geocode_candidates_id_idx
  ON listings (id)
  WHERE geom IS NULL AND locality IS NOT NULL AND geocode_attempted_at IS NULL;

CREATE INDEX IF NOT EXISTS listings_street_name_key_null_id_idx
  ON listings (id)
  WHERE street_name_key IS NULL AND street IS NOT NULL AND street <> '';

CREATE INDEX IF NOT EXISTS listings_geo_cell_key_byt_null_id_idx
  ON listings (id)
  WHERE geo_cell_key IS NULL AND category_main = 'byt'
    AND geom IS NOT NULL AND obec_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS listings_geo_cell_key_null_id_idx
  ON listings (id)
  WHERE geo_cell_key IS NULL
    AND category_main = ANY (ARRAY['dum','pozemek','komercni','ostatni'])
    AND geom IS NOT NULL AND obec_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS listings_source_active_street_id_idx
  ON listings (source, id)
  WHERE street IS NULL AND is_active;

DROP INDEX IF EXISTS listings_geocode_candidates_idx;
DROP INDEX IF EXISTS listings_street_name_key_null_idx;
DROP INDEX IF EXISTS listings_geo_cell_key_byt_null_idx;
DROP INDEX IF EXISTS listings_geo_cell_key_null_idx;
DROP INDEX IF EXISTS listings_source_active_street_idx;
