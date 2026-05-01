-- 002_image_storage.sql
-- Adds columns to track R2 download state per image.
--
-- storage_path             - bucket key after successful upload (NULL = not yet stored)
-- download_attempts        - number of failed/successful download attempts
-- last_download_attempt_at - timestamp of last attempt; NULL = never tried
--
-- Image rows where storage_path IS NULL AND download_attempts < 5 are
-- candidates for the next image-download phase. After 5 failed attempts
-- we give up to avoid wasting cycles on permanently-dead URLs.

alter table images
  add column storage_path             text,
  add column download_attempts        integer not null default 0,
  add column last_download_attempt_at timestamptz;

create index on images (storage_path)
  where storage_path is null;
