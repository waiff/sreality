-- 350: enforce images.listing_id / listing_videos.listing_id NOT NULL (Gate-2 backstop).
--
-- The R2 child-media writers (scraper/db.record_images / record_videos) dedupe on
-- ON CONFLICT (listing_id, sequence). A NULL listing_id NEVER conflicts, so an
-- unresolved FK would silently spawn an UNBOUNDED duplicate row on every refetch
-- (plus orphan R2 bytes) — the exact failure the arbiter switch to listing_id was
-- meant to prevent. The code fix (this migration's same PR) makes both writers ALWAYS
-- carry a resolved surrogate listings.id: the portal chokepoint record_media passes it
-- directly; sreality's own callers look it up from their always-present sreality_id.
-- This CHECK is the DB-level guarantee behind that invariant — a NULL FK becomes a loud
-- abort instead of a silent duplicate generator.
--
-- Deploy ordering, mirroring migration 314: the code above is safe to run BEFORE this
-- migration (today every write already resolves a non-NULL listing_id, so 0 NULL rows),
-- and this migration must be in place BEFORE the Gate-2 flip lets an old worker attempt
-- a NULL. Verified pre-apply: 0 rows with NULL listing_id in either table.
--
-- images is hot (~8.3M rows, the image drain writes continuously); listing_videos less
-- so. Both follow migration 313/314's online idiom: a validated NOT-NULL CHECK, not an
-- ALTER COLUMN SET NOT NULL rewrite. ADD ... NOT VALID takes a brief SHARE ROW EXCLUSIVE
-- lock (bounded by lock_timeout); VALIDATE is SHARE UPDATE EXCLUSIVE, non-blocking to the
-- concurrent media writes.

SET lock_timeout = '8s';

ALTER TABLE images
    ADD CONSTRAINT images_listing_id_present_check CHECK (listing_id IS NOT NULL) NOT VALID;
ALTER TABLE images VALIDATE CONSTRAINT images_listing_id_present_check;

ALTER TABLE listing_videos
    ADD CONSTRAINT listing_videos_listing_id_present_check CHECK (listing_id IS NOT NULL) NOT VALID;
ALTER TABLE listing_videos VALIDATE CONSTRAINT listing_videos_listing_id_present_check;
