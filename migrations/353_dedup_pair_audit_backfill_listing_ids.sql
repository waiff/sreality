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
-- Verified live 2026-07-22: 90,498 rows, 4,542 with BOTH *_listing_id NULL (0 with
-- exactly one NULL), and every one of those 4,542 carries a non-NULL *_sreality_id
-- that still resolves to a live listing — so one set-based UPDATE fills them all.
-- COALESCE keeps any already-populated side and leaves a side NULL only if its
-- sreality_id no longer resolves (none today) — the backfill must never abort on that.
-- Idempotent + re-runnable. lock_timeout: ~90k rows is not tiny but not hot — fail
-- fast and retry rather than queue behind a concurrent audit writer.

SET lock_timeout = '5s';

UPDATE dedup_pair_audit a
   SET left_listing_id = COALESCE(
           a.left_listing_id,
           (SELECT l.id FROM listings l WHERE l.sreality_id = a.left_sreality_id)),
       right_listing_id = COALESCE(
           a.right_listing_id,
           (SELECT l.id FROM listings l WHERE l.sreality_id = a.right_sreality_id))
 WHERE a.left_listing_id IS NULL OR a.right_listing_id IS NULL;

RESET lock_timeout;
