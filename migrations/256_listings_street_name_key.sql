-- 256_listings_street_name_key.sql
-- Stored dedup street-group NAME key, so the real-time dedup --dirty drain can
-- SCOPE its eligible load to the claimed properties' street groups instead of
-- scanning the whole market every run.
--
-- WHY: the dedup engine's --dirty drain (Wave 4c, migration 242) re-decides only
-- the street groups that touch a just-dedup-ready property — O(dirty) pair-work —
-- but `_load_eligible` still loaded ALL ~100k eligible rows every run (O(market)),
-- because the street name key was computed in Python (toolkit.dedup_engine /
-- scraper.street.street_name_key) and not stored, so the load could not be
-- filtered in SQL. A tagging flood (a new portal's CLIP backfill) enqueues most of
-- the market, and the unbounded full load then drops the pooled connection mid-run.
-- This column stores that key so the --dirty load can be scoped:
--   WHERE <eligibility>
--     AND ( street_id = ANY(:claimed_street_ids)
--           OR (coalesce(obec_id,-1), street_name_key) IN (:claimed_name_keys) )
-- Street groups are obec-bounded (the name key is scoped by obec_id; a street_id
-- is one physical street), so that scoped load is COMPLETE — every member of a
-- group that contains a claimed-dirty property is loaded, peers included.
--
-- It is a PYTHON-derived column (the normalization folds diacritics + strips
-- street-words + trailing house-number tokens — not faithfully reproducible in
-- SQL without drift), populated at every street-write path
-- (scraper.db.upsert_listing / write_detail_batch + the street backfills) and by
-- scripts.backfill_street_name_key for the existing rows. A plain nullable column,
-- so ADD COLUMN is metadata-only (no table rewrite). OUT of the content hash (like
-- street / house_number / zip), so backfilling it never writes a snapshot. A
-- parity test asserts stored == recomputed; the 6h full scan (which recomputes the
-- key live) is the backstop if a key ever goes stale.

ALTER TABLE listings ADD COLUMN IF NOT EXISTS street_name_key text;

COMMENT ON COLUMN listings.street_name_key IS
  'Derived dedup street-group name key (scraper.street.street_name_key(street)); '
  'powers the dedup --dirty drain''s scoped eligible load. Out of the content hash; '
  'a parity test guards stored == recomputed.';

-- Name-key arm of the scoped load: (coalesce(obec_id,-1), street_name_key) IN (...).
-- An EXPRESSION index on coalesce(obec_id,-1) (NOT plain obec_id) so the ~0.4% of
-- eligible rows with a NULL obec_id — which group as `name:None:<key>` in the engine
-- — are matchable too (a plain obec_id index + IN can't match NULL), keeping the
-- scoped load complete with no asterisk. Partial on the SAME eligibility predicate
-- migration 127 uses, so it stays small (~100k rows) and never indexes the
-- streetless / disposition-less majority.
CREATE INDEX IF NOT EXISTS listings_dedup_name_key_idx
  ON listings ((coalesce(obec_id, -1)), street_name_key)
  WHERE street IS NOT NULL AND street <> '' AND disposition IS NOT NULL;

-- The street_id arm (street_id = ANY(...)) is served by migration 127's
-- listings_dedup_eligible_idx (street_id, disposition) WHERE <same eligibility> —
-- street_id leads it, so an ANY() lookup over the eligible set uses that index. No
-- new street_id index is needed (validated by EXPLAIN against prod).

-- One-shot backfill index (migration 240 precedent): scripts.backfill_street_name_key
-- keyset-paginates the street-bearing rows whose key is still NULL. This partial index
-- keeps each chunk's `sreality_id > cursor ... LIMIT n` tight as the NULL tail depletes,
-- and SELF-EMPTIES as the column fills (zero rows match once the backfill completes), so
-- it costs nothing to leave behind.
CREATE INDEX IF NOT EXISTS listings_street_name_key_null_idx
  ON listings (sreality_id)
  WHERE street_name_key IS NULL AND street IS NOT NULL AND street <> '';
