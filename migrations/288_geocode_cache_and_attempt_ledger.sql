-- 288_geocode_cache_and_attempt_ledger.sql
-- Forward-geocoding gets the same durability rails the RÚIAN street resolver already
-- has (migrations 196/222/263): a persistent result cache and a per-row attempt stamp.
--
-- WHY (2026-07 location audit): address→coords enrichment was per-portal patchwork —
-- idnes/realitymix geocode only inside a detail refetch (the standing no-geom stock is
-- never re-attempted: 842/842 idnes and 1,245/1,245 realitymix no-geom rows show no
-- attempt), maxima/remax/mmreality/ceskereality have no geocode path at all, and no
-- attempt ledger exists, so "attempted but failed" is indistinguishable from "never
-- tried" and every backfill re-risks the 2026-06 250k-credit Mapy incident. The one
-- prior ledger (the realitymix backfill's raw_json.coords.geocode_backfill stamp) has
-- exactly the clobber-on-refetch weakness migration 263 fixed for streets: the next
-- detail fetch rebuilds raw_json from the page and destroys the stamp. Durable state
-- goes in columns.
--
-- geocode_cache: one row per normalized query string (Python-normalized by
-- scraper.location — lowercased, whitespace-collapsed; no SQL twin of the
-- normalization exists or is needed, callers look keys up by exact match). Rows with
-- lat/lng NULL are negative cache (query returned nothing usable / too coarse);
-- scraper.location retries those after a TTL, positive rows are permanent. Backend-only
-- cost-control mirror, not history — safe to TRUNCATE (worst case: re-spent credits).
CREATE TABLE IF NOT EXISTS geocode_cache (
  query_key    text PRIMARY KEY,
  lat          double precision,
  lng          double precision,
  matched_type text,
  confidence   text,
  resolved_at  timestamptz NOT NULL DEFAULT now()
);

-- Internal object: RLS on, no grants (only service-role writers touch it).
ALTER TABLE geocode_cache ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE geocode_cache IS
  'Persistent Mapy.cz forward-geocode cache (migration 288), keyed by the '
  'Python-normalized query string (scraper.location). lat/lng NULL = negative '
  'cache, retried after a TTL. A cost-control mirror, not history.';

-- The row-grain attempt ledger for LISTINGS geocoding, mirroring
-- coord_street_attempt_version (migration 222) in timestamp form: the backfill
-- stamps every row it processes — placed, miss, or too-coarse — so re-runs skip it
-- and the candidate pool self-empties. NOT in LISTING_COLUMNS, so ingest upserts
-- never touch it (the mig-263 lesson: durable provenance lives in columns the
-- refetch can't destroy). Out of the content hash; never snapshot-churning.
ALTER TABLE listings ADD COLUMN IF NOT EXISTS geocode_attempted_at timestamptz;

COMMENT ON COLUMN listings.geocode_attempted_at IS
  'When the forward-geocode backfill last processed this row (migration 288) — '
  'stamped on success AND failure so the candidate pool self-empties. NULL = '
  'never attempted. Independent of the drain-path geocode fallback, which only '
  'fires inside a detail fetch.';

-- Candidate seek index for the backfill (the mig-276 backfill-index pattern):
-- keeps each `WHERE geom IS NULL AND locality IS NOT NULL AND geocode_attempted_at
-- IS NULL ... ORDER BY sreality_id LIMIT n` batch an index seek, and SELF-EMPTIES
-- as rows are stamped or placed, so it costs ~nothing to leave behind.
CREATE INDEX IF NOT EXISTS listings_geocode_candidates_idx
  ON listings (sreality_id)
  WHERE geom IS NULL
    AND locality IS NOT NULL
    AND geocode_attempted_at IS NULL;
