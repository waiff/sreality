-- 296_byt_geo_cell_key.sql
-- Byt geo rung B: extend the stored dedup blocking-cell key (migration 276) to
-- APARTMENTS, so street-less byt stop being structurally invisible to dedup.
--
-- WHY: a byt with no parsed street fails the street pass's rule-A eligibility
-- (street + disposition), and byt is deliberately EXCLUDED from the geo families
-- (one building stacks many units on one coordinate — coord alone would false-merge
-- them). Measured 2026-07-11: ~19.3k active byt were reachable by NEITHER pass. The
-- new rung blocks them on "geo cell + disposition": this migration stamps the cell,
-- the engine shards each cell by disposition class in Python (the same loss-free
-- shard the street pass uses — the shard is NOT part of the stored key, exactly as
-- street groups store street_name_key and shard at load), and classify_byt_geo_pair
-- emits CANDIDATES ONLY — no attribute signal ever auto-merges on this rung; pHash /
-- forensic-High stay the sole merge gates (rule #15).
--
-- KEY DEFINITION — unchanged format 'geo:{obec_id}:{lat4}:{lng4}:{bucket}:{category_type}':
--   * now defined for FIVE families: the four geo families PLUS 'byt'. byt gets its
--     OWN bucket 'byt' (the ELSE branch below — it NEVER collapses into 'dum|komercni',
--     so a flat can never co-cell with a house/commercial and reach the wrong
--     classifier). The Python twin (toolkit.publication.CELL_FAMILIES = GEO_FAMILIES
--     + ('byt',)) is pinned to this function body by a unit test, exactly like
--     migration 276's list is pinned to GEO_FAMILIES.
--   * still VOLATILE-PREDICATE-FREE (mirror of 276's design): eligibility
--     (is_active, area, disposition IS NOT NULL) stays a LOAD-TIME filter in
--     _load_geo_eligible / the BYT_GEO_ELIGIBLE_PREDICATE, so a delist/relist or a
--     late-parsed disposition never churns the stored key.
--   * the existing trigger (trg_listings_geo_cell_key, migration 276) already fires
--     on INSERT OR UPDATE OF geom/obec_id/category_main/category_type and calls this
--     function — redefining the function is the whole write-path change; new byt rows
--     stamp their key from the moment this applies.
--
-- NO IN-MIGRATION BACKFILL (mig-276 pattern: ~107k ACTIVE byt rows + the inactive
-- history are far too heavy for one MCP statement). Canonical batched backfill, run
-- out-of-band right after applying (repeat until UPDATE 0; the partial NULL index
-- below keeps each batch an index seek and SELF-EMPTIES as the column fills):
--
--   UPDATE listings
--      SET geo_cell_key = public.listing_geo_cell_key(
--            obec_id, geom, category_main, category_type)
--    WHERE sreality_id IN (
--          SELECT sreality_id FROM listings
--           WHERE geo_cell_key IS NULL
--             AND category_main = 'byt'
--             AND geom IS NOT NULL AND obec_id IS NOT NULL
--           ORDER BY sreality_id
--           LIMIT 20000);
--
-- geo_cell_key is derived, never part of the scraped-content hash, so the backfill
-- writes no snapshots (rule #2 untouched). Until it completes, the loaders skip
-- NULL-key byt rows — a not-yet-stamped listing merely waits, never mis-groups.
--
-- The 276 backfill index (listings_geo_cell_key_null_idx) is category-limited to the
-- four geo families (and may already have been dropped after self-emptying), so byt
-- gets its OWN self-emptying backfill index below.

-- The single definition (replaces migration 276's four-family body; same signature,
-- still IMMUTABLE/PARALLEL SAFE — the trigger + backfill + scoped loads all call it).
CREATE OR REPLACE FUNCTION public.listing_geo_cell_key(
  p_obec_id bigint,
  p_geom geography,
  p_category_main text,
  p_category_type text
) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE
    WHEN p_obec_id IS NULL OR p_geom IS NULL
         OR p_category_main IS NULL
         OR p_category_main NOT IN ('dum', 'pozemek', 'komercni', 'ostatni', 'byt')
    THEN NULL
    ELSE 'geo:' || p_obec_id
      || ':' || trim_scale(round(ST_Y(p_geom::geometry)::numeric, 4))::text
      || ':' || trim_scale(round(ST_X(p_geom::geometry)::numeric, 4))::text
      || ':' || CASE WHEN p_category_main IN ('dum', 'komercni')
                     THEN 'dum|komercni' ELSE p_category_main END
      || ':' || coalesce(p_category_type, 'None')
  END
$$;

COMMENT ON COLUMN listings.geo_cell_key IS
  'Trigger-maintained dedup blocking cell (public.listing_geo_cell_key: obec + '
  '4dp-rounded coordinate + category bucket + offering); the single definition of '
  'the cell-blocked families'' key (migrations 276 + 296). Stamped for the four geo '
  'families AND byt (own ''byt'' bucket — the byt geo rung shards it by disposition '
  'at load). NULL outside those categories or without obec_id/geom. Derived — never '
  'in the content hash.';

-- One-shot BYT backfill index (mig 240/256/276 precedent): keeps each backfill
-- batch's `WHERE geo_cell_key IS NULL AND category_main = 'byt' ... ORDER BY
-- sreality_id LIMIT n` an index seek, and SELF-EMPTIES once the backfill completes
-- (zero rows match), so it costs nothing to leave behind.
CREATE INDEX IF NOT EXISTS listings_geo_cell_key_byt_null_idx
  ON listings (sreality_id)
  WHERE geo_cell_key IS NULL
    AND category_main = 'byt'
    AND geom IS NOT NULL AND obec_id IS NOT NULL;

-- The format CHECK (listings_geo_cell_key_format, migration 276) already covers the
-- byt keys — the function only ever emits 'geo:'-prefixed text or NULL. No new
-- constraint, no trigger change.
