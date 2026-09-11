-- 496_images_rendition_provenance.sql
-- Rendition provenance for the bytes stored in R2 — the precondition for
-- re-mastering the sreality photo corpus.
--
-- THE PROBLEM. scraper/image_storage.py downloads sreality photos through the
-- CDN transform "res,749,562,3|shr,,20|jpg,90": mode 3 CROPS the frame to 4:3
-- and 749px is far below the master. Their "res,1800,1800,1|shr,,20|jpg,80"
-- template returns the UNCROPPED native photo up to 1800px instead. A follow-up
-- PR switches the download template; another re-downloads the already-stored
-- sreality objects at the new template, OVERWRITING each row's existing R2
-- object (images.storage_path is reused, never recomputed). Once bytes can be
-- replaced under a stable key, "what is actually in this object?" stops being
-- derivable from the code that ran at download time — it has to be a stored
-- fact. That fact is images.rendition.
--
-- images.rendition — WHAT bytes the stored object holds:
--   NULL                  downloaded before provenance existed. For a sreality
--                         row that means the legacy 749x562 mode-3 crop; for
--                         any other portal it means the portal's native file.
--   'sreality-1800-fit'   the sreality master via their SQUARE_1800_JPG
--                         template (res,1800,1800,1|shr,,20|jpg,80) — whole
--                         frame, no crop, no watermark.
--   'sreality-749-crop'   assessed by the re-master lane, source gone, legacy
--                         crop retained. TERMINAL: the lane never reconsiders a
--                         row stamped this, so a dead source cannot be retried
--                         forever.
--   'native'              a non-sreality portal's own file. Reserved: the
--                         download path gains the keyword in this migration
--                         (scraper/db.mark_image_stored) but no caller passes it
--                         yet, so nothing writes 'native' until the download PR
--                         lands — a non-sreality row stays NULL until then.
--
-- images.stored_width / images.stored_height — the DECODED pixel size measured
-- at upload, so "did the re-master actually land bigger bytes?" is answerable
-- in SQL without touching R2.
--
-- The re-master lane's pending predicate, verbatim:
--   rendition IS NULL AND storage_path IS NOT NULL AND sreality_url LIKE '%sdn.cz%'
-- It deliberately gets NO index: the lane walks sreality listings in id pages
-- (listings.source, then images by listing_id through the existing
-- (listing_id, sequence) unique index), so a corpus-wide pass costs a few
-- hundred cheap lookups and no hot-table index build — "the queue is the
-- cursor", with the rendition column as the queue.
--
-- APPLY SHAPE. Purely additive and idempotent (IF NOT EXISTS / CREATE OR
-- REPLACE): applies unchanged through the Supabase MCP (`apply_migration`) or
-- through `.github/workflows/apply_migration.yml` (`psql -f`, statement
-- autocommit). Nothing here needs a transaction and a re-apply is a no-op.
-- No grants: images_public's ACL is authenticated-SELECT-only and CREATE OR
-- REPLACE VIEW preserves it. Three new nullable columns, one trailing view
-- column.

SET lock_timeout = '5s';
SET statement_timeout = '30min';

ALTER TABLE images
  ADD COLUMN IF NOT EXISTS rendition text,
  ADD COLUMN IF NOT EXISTS stored_width integer,
  ADD COLUMN IF NOT EXISTS stored_height integer;

COMMENT ON COLUMN images.rendition IS
  'What bytes the stored R2 object holds. NULL = pre-provenance download (sreality: the legacy 749x562 mode-3 crop; other portals: the native file). ''sreality-1800-fit'' = sreality master via res,1800,1800,1|shr,,20|jpg,80 (whole frame, no watermark). ''sreality-749-crop'' = assessed by the re-master lane, source gone, legacy crop retained (TERMINAL). ''native'' = a non-sreality portal''s own file.';

COMMENT ON COLUMN images.stored_width IS
  'Decoded pixel width of the stored R2 object, measured at upload. NULL = never measured.';

COMMENT ON COLUMN images.stored_height IS
  'Decoded pixel height of the stored R2 object, measured at upload. NULL = never measured.';

-- Migration 335's body byte-for-byte, with i.rendition appended as the LAST
-- column so every existing ordinal is untouched.
CREATE OR REPLACE VIEW images_public AS
 SELECT i.id,
    i.sreality_id,
    i.sequence,
    i.sreality_url,
    i.storage_path,
    ct.fine_tag AS clip_fine_tag,
    ct.logical_tag AS clip_logical_tag,
    ct.confidence AS clip_confidence,
    ct.render_score AS clip_render_score,
    i.phash,
    i.listing_id,
    i.rendition
   FROM images i
     LEFT JOIN LATERAL ( SELECT t.fine_tag,
            t.logical_tag,
            t.confidence,
            t.render_score
           FROM image_clip_tags t
          WHERE t.image_id = i.id
          ORDER BY t.tagged_at DESC
         LIMIT 1) ct ON true;
