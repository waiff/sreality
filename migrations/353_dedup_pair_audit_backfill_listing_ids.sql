-- 353_dedup_pair_audit_backfill_listing_ids.sql
-- Backfill dedup_pair_audit.left_listing_id / right_listing_id — the R2 surrogate
-- mirrors (listings.id) of the legacy *_sreality_id columns — on the rows written
-- before those columns existed.
--
-- MUST precede the Gate-2 reader repoint (api/property_dedup.py's phash_audit /
-- clip_coverage drive their image joins off *_listing_id now, and the engine's
-- dismissal-dedupe keys on it): a legacy row left with NULL *_listing_id would drop
-- out of every one of those readers. The engine + operator WRITERS already stamp the
-- surrogate directly at insert time (same PR), so this only heals the historical tail.
--
-- Additive data backfill on an append-only table (rule #9-adjacent); no schema change.
--
-- SELF-PAIR GUARD (`left_sreality_id <> right_sreality_id`): verified live 2026-07-22
-- that ALL 4,542 rows with NULL *_listing_id are degenerate SELF-PAIRS
-- (left_sreality_id = right_sreality_id — a listing recorded against itself, e.g. a
-- dual-keyed listing appearing in both its id: and name: groups; 0 rows are genuine
-- distinct pairs). Two reasons this guard is mandatory, not cosmetic:
--   1. dedup_pair_audit carries `CHECK (left_sreality_id <> right_sreality_id) NOT
--      VALID` — grandfathered over those self-pairs. Any UPDATE re-checks it on the
--      touched row, so an unguarded backfill ABORTS on the first self-pair (23514).
--   2. A self-pair resolves both sides to the SAME listings.id, which is meaningless
--      evidence — the readers are correctly better off dropping it (NULL *_listing_id)
--      than rendering a listing paired with itself.
-- sreality_id -> listings.id is 1:1 (unique index), so `left_sreality_id <>
-- right_sreality_id` is exactly equivalent to "the two sides resolve to distinct
-- listings" — the only rows it is safe and useful to fill. COALESCE keeps any
-- already-populated side and never overwrites. Idempotent + re-runnable. lock_timeout:
-- ~90k rows is not tiny but not hot — fail fast and retry rather than queue behind a
-- concurrent audit writer. (Today this fills 0 rows — every NULL row is a self-pair —
-- but it stays correct for any future legitimate legacy row.)

SET lock_timeout = '5s';

UPDATE dedup_pair_audit a
   SET left_listing_id = COALESCE(
           a.left_listing_id,
           (SELECT l.id FROM listings l WHERE l.sreality_id = a.left_sreality_id)),
       right_listing_id = COALESCE(
           a.right_listing_id,
           (SELECT l.id FROM listings l WHERE l.sreality_id = a.right_sreality_id))
 WHERE (a.left_listing_id IS NULL OR a.right_listing_id IS NULL)
   AND a.left_sreality_id IS DISTINCT FROM a.right_sreality_id;

RESET lock_timeout;
