-- 276_listings_geo_cell_key.sql
-- Stored dedup geo blocking-cell key, trigger-maintained — THE single definition of
-- the geo family's blocking cell (mirrors migration 256's stored street_name_key).
--
-- WHY: the geo dedup path (rule #15: single-dwelling dum/pozemek/komercni/ostatni)
-- blocked pairs on a cell key computed in PYTHON per row at scan time
-- (toolkit.dedup_engine.geo_cell_key inside scripts.dedup_engine._load_geo_eligible),
-- so the cell was invisible to SQL: the geo pass could only ever FULL-SCAN the
-- eligible set, and a real-time geo dirty drain (the street path's Wave 4c posture,
-- migrations 242/256/258) had no column to scope its load by. Storing the key
-- (a) makes SQL the single definition — the loader now SELECTs it verbatim and the
-- Python function is retired from the load path (kept only as executable
-- documentation of the format), and (b) is the foundation for the real-time geo
-- path (PR-B): a dirty geo load becomes an index seek on this column, exactly like
-- the street --dirty lane seeks (obec_id, street_name_key).
--
-- Unlike street_name_key (Python-derived: the diacritic fold isn't worth twinning in
-- SQL — see 256/264), this key is PURE SQL over four columns already on the row, so a
-- trigger maintains it and no write-path enumeration / parity job is needed: the
-- trigger IS the write path.
--
-- KEY DEFINITION — 'geo:{obec_id}:{lat4}:{lng4}:{bucket}:{category_type}':
--   * defined only for the four geo families ('dum','pozemek','komercni','ostatni')
--     with BOTH obec_id and geom present; everything else (byt, foreign points,
--     un-PIP'd rows) stays NULL. Deliberately INDEPENDENT of the volatile loader
--     predicates (is_active, area, NOT-street-eligible) — eligibility remains a
--     load-time filter, so a delist/relist or a late-parsed street never churns the
--     stored key.
--   * lat/lng come from geom (ST_Y/ST_X — listings has no scalar lat/lng columns;
--     those live on properties, migration 250), rounded to 4 dp as
--     trim_scale(round(x::numeric, 4))::text. NOTE the rendering is NOT guaranteed
--     byte-identical to the retired Python f-string (numeric round is half-away-from-
--     zero vs Python's bankers' rounding on exact .00005 ties; trim_scale drops
--     trailing zeros like Python for fractional values but renders a whole number as
--     '50' where Python said '50.0'). That is fine BY DESIGN: the stored column is
--     the new single definition, every row is stamped by this one function (trigger +
--     backfill both call it), and nothing compares stored keys against Python-built
--     ones. Internal consistency is the only requirement.
--   * bucket collapses dum+komercni into 'dum|komercni' (the one sanctioned
--     cross-type co-locates in one cell, toolkit.dedup_engine.geo_category_bucket);
--     a NULL category_type renders 'None' (parity with the Python f-string) so a
--     ct-less row still groups deterministically rather than going NULL.
--
-- TRIGGER ORDERING: obec_id is set by trg_listings_admin_geo (BEFORE INSERT OR
-- UPDATE OF geom, migrations 140/162/222 — the only other BEFORE trigger on
-- listings). Postgres fires same-event BEFORE triggers in NAME order, and
-- 'trg_listings_admin_geo' < 'trg_listings_geo_cell_key' byte-wise ('a' < 'g'), so on
-- every INSERT / UPDATE OF geom the admin-geo PIP has already stamped NEW.obec_id by
-- the time this trigger reads it. The one asymmetry: admin-geo is gated
-- WHEN (new.geom IS NOT NULL), so an UPDATE that NULLs geom leaves a stale
-- NEW.obec_id — harmless here, because a NULL geom forces the key to NULL regardless.
-- This trigger also listens on obec_id/category_main/category_type directly, so a
-- backfill that writes obec_id without touching geom re-keys the row too.
--
-- NO IN-MIGRATION BACKFILL (~450k listings is too heavy for one MCP statement).
-- Canonical batched backfill, run out-of-band right after applying (repeat until
-- UPDATE 0; the partial NULL index below keeps each batch an index seek and
-- self-empties as the column fills):
--
--   UPDATE listings
--      SET geo_cell_key = public.listing_geo_cell_key(
--            obec_id, geom, category_main, category_type)
--    WHERE sreality_id IN (
--          SELECT sreality_id FROM listings
--           WHERE geo_cell_key IS NULL
--             AND category_main IN ('dum', 'pozemek', 'komercni', 'ostatni')
--             AND geom IS NOT NULL AND obec_id IS NOT NULL
--           ORDER BY sreality_id
--           LIMIT 20000);
--
-- geo_cell_key is a derived column, never part of the scraped-content hash, so the
-- backfill writes no snapshots (rule #2 untouched). Until the backfill completes,
-- _load_geo_eligible skips NULL-key rows — a not-yet-stamped listing merely waits,
-- it is never mis-grouped.

ALTER TABLE listings ADD COLUMN IF NOT EXISTS geo_cell_key text;

COMMENT ON COLUMN listings.geo_cell_key IS
  'Trigger-maintained dedup geo blocking cell (public.listing_geo_cell_key: obec + '
  '4dp-rounded coordinate + category bucket + offering); the single definition of '
  'the geo family''s blocking key (migration 276). NULL outside the four geo '
  'categories or without obec_id/geom. Derived — never in the content hash.';

-- The single definition. Trigger + backfill + any future scoped load all call this;
-- IMMUTABLE (pure function of its arguments) so it could even back an expression
-- index if one is ever needed.
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
         OR p_category_main NOT IN ('dum', 'pozemek', 'komercni', 'ostatni')
    THEN NULL
    ELSE 'geo:' || p_obec_id
      || ':' || trim_scale(round(ST_Y(p_geom::geometry)::numeric, 4))::text
      || ':' || trim_scale(round(ST_X(p_geom::geometry)::numeric, 4))::text
      || ':' || CASE WHEN p_category_main IN ('dum', 'komercni')
                     THEN 'dum|komercni' ELSE p_category_main END
      || ':' || coalesce(p_category_type, 'None')
  END
$$;

CREATE OR REPLACE FUNCTION public.listings_set_geo_cell_key()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.geo_cell_key := public.listing_geo_cell_key(
    NEW.obec_id, NEW.geom, NEW.category_main, NEW.category_type);
  RETURN NEW;
END;
$$;

-- Named to sort AFTER trg_listings_admin_geo (see TRIGGER ORDERING above).
DROP TRIGGER IF EXISTS trg_listings_geo_cell_key ON listings;
CREATE TRIGGER trg_listings_geo_cell_key
  BEFORE INSERT OR UPDATE OF geom, obec_id, category_main, category_type ON listings
  FOR EACH ROW
  EXECUTE FUNCTION public.listings_set_geo_cell_key();

-- The blocking-cell seek index for the geo loads (PR-B's dirty path seeks it
-- directly; the full geo pass groups on it). Partial: only the four geo families
-- ever carry a key, so this stays small.
CREATE INDEX IF NOT EXISTS listings_geo_cell_key_idx
  ON listings (geo_cell_key)
  WHERE geo_cell_key IS NOT NULL;

-- One-shot backfill index (migrations 240/256 precedent): keeps each backfill
-- batch's `WHERE geo_cell_key IS NULL ... ORDER BY sreality_id LIMIT n` an index
-- seek, and SELF-EMPTIES once the backfill completes (zero rows match), so it costs
-- nothing to leave behind.
CREATE INDEX IF NOT EXISTS listings_geo_cell_key_null_idx
  ON listings (sreality_id)
  WHERE geo_cell_key IS NULL
    AND category_main IN ('dum', 'pozemek', 'komercni', 'ostatni')
    AND geom IS NOT NULL AND obec_id IS NOT NULL;

-- Cheap format invariant (migration 264's guard pattern, simplified): the trigger
-- only ever writes 'geo:'-prefixed keys or NULL, so any other value is a bug (e.g. a
-- hand-rolled backfill writing garbage — direct geo_cell_key UPDATEs bypass the
-- trigger, whose UPDATE OF list deliberately excludes the column itself). NOT VALID
-- + VALIDATE in one MCP transaction holds the ACCESS EXCLUSIVE lock through the
-- validation scan (~1-2s at current size, accepted — see 264's lock note); the scan
-- is trivially green pre-backfill (all NULL).
ALTER TABLE listings
  ADD CONSTRAINT listings_geo_cell_key_format
  CHECK (geo_cell_key IS NULL OR geo_cell_key LIKE 'geo:%')
  NOT VALID;

ALTER TABLE listings VALIDATE CONSTRAINT listings_geo_cell_key_format;
