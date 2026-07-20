-- 314: Complete + enforce the (source, source_id_native) natural key.
--
-- source_id_native (migration 091) is the portal-native half of the true natural
-- key the listing-identity refactor (docs/design/listing-identity-refactor.md)
-- leans on as "the true natural key already exists". It did not: the sreality
-- detail-drain (scraper/db.write_detail_batch — the primary sreality write path
-- since the cadence split, migration 105) never stamped it, so new sreality rows
-- accumulated NULLs (35 at the audit, 396 and climbing by the time this shipped).
-- Phase 0 (migration 311) planned this backfill + enforcement but never carried it.
--
-- The code fix ships in the SAME PR (scraper/db.py: upsert_listing,
-- ingest_scraped_listing, and write_detail_batch now stamp source_id_native inline
-- at INSERT) and MUST be deployed to Railway BEFORE this migration is applied —
-- otherwise the NOT-NULL CHECK below would reject the old worker's inserts.
--
-- Enforcement mirrors migration 313's `listings_id_present_check`: a validated
-- NOT-NULL CHECK rather than ALTER COLUMN SET NOT NULL, so there is no
-- ACCESS EXCLUSIVE full-table rewrite/scan and the always-on worker keeps writing.
-- The ADD ... NOT VALID takes a brief SHARE ROW EXCLUSIVE lock (apply under
-- lock_timeout + retry); VALIDATE is SHARE UPDATE EXCLUSIVE (non-blocking to writes).

-- 1. Heal residual NULLs. All live NULLs are sreality, whose native id IS its
--    sreality_id; the source guard keeps this from ever fabricating a wrong id for
--    a hypothetical non-sreality NULL (there are none — the ingest path always sets it).
UPDATE listings
   SET source_id_native = sreality_id::text
 WHERE source_id_native IS NULL
   AND source = 'sreality';

-- 2. Enforced-on-every-write immediately; safe because the deployed code above
--    already stamps source_id_native on every INSERT path.
ALTER TABLE listings
  ADD CONSTRAINT listings_source_id_native_present
  CHECK (source_id_native IS NOT NULL) NOT VALID;

-- 3. Prove the existing rows satisfy it (non-blocking to writes).
ALTER TABLE listings VALIDATE CONSTRAINT listings_source_id_native_present;

-- With this + the migration-091 UNIQUE (source, source_id_native) index,
-- (source, source_id_native) is a complete, enforced natural key.
