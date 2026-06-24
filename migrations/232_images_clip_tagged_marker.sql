-- CLIP tagging needs an index-fast way to find "images not yet tagged", the same
-- way image DOWNLOAD finds "images not yet stored" via the storage_path-NULL partial
-- indexes (images_unstored_by_listing_idx etc.). "Untagged" is otherwise a cross-table
-- anti-join against image_clip_tags with no in-table marker, so the selector had to
-- SEQ-SCAN all ~5.2M images every run + sort — which intermittently blew the statement
-- timeout (the failed clip_tag shards) and made region-prioritised tagging impossible.
--
-- Mirror the download pattern: a denormalised in-table marker `clip_tagged_at` (set by
-- the tagger in the same write that upserts image_clip_tags), plus two PARTIAL indexes
-- over only the not-yet-tagged set:
--   *_sid_idx — drive from a kraj's listings (listings_region_id_idx) into its untagged
--               images by sreality_id (region-prioritised queuing).
--   *_id_idx  — id-ordered global fallback (newest untagged first), no sort, top-N.
-- Both shrink as coverage grows, so tagging only gets cheaper. On prod the indexes are
-- built CONCURRENTLY out of band; the plain form here is for fresh rebuilds (empty table).
alter table images add column if not exists clip_tagged_at timestamptz;

create index if not exists images_needs_clip_sid_idx
  on images (sreality_id)
  where clip_tagged_at is null and storage_path is not null;

create index if not exists images_needs_clip_id_idx
  on images (id)
  where clip_tagged_at is null and storage_path is not null;
