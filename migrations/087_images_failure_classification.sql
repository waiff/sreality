-- 087_images_failure_classification.sql
--
-- Two new columns on `images` so the download phase can tell apart
-- "the listing was taken down before we got the bytes" from "everything
-- else", and so we keep the last error message for diagnosis.
--
--   unavailable_reason — set to 'listing_taken_down' once a freshness
--     check confirms the parent listing returns 404/410 from sreality.
--     Free-text (not enum) so future reasons can be added without a
--     migration. NULL = still actionable.
--   last_error — exception message from the most recent download
--     attempt, truncated to 500 chars. Same pattern as
--     listing_fetch_failures.last_error (001_initial.sql).
--
-- The partial index keeps `pending_image_downloads` scans cheap as the
-- backlog drains: we only ever care about rows that are still
-- actionable (no storage_path yet, no terminal unavailable_reason).

alter table images
  add column unavailable_reason text,
  add column last_error text;

create index if not exists images_pending_actionable_idx
  on images (id)
  where storage_path is null and unavailable_reason is null;
