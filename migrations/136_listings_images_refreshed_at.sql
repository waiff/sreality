-- Stale-image-URL refresh sweep (scripts.refresh_stale_image_urls / refresh_stale_images.yml).
--
-- Portals rotate image CDN URLs over time. A listing whose photos we never downloaded
-- to R2 before its URLs rotated is otherwise stuck: the stored URL 404s, the frontend
-- fallback can't load it, and the image downloader can't fetch it either. The sweep
-- re-enqueues such active listings for a detail re-fetch so `db.record_images` repoints
-- their not-yet-stored image URLs to the current ones, after which the image backfill
-- (images.yml) can store them.
--
-- `images_refreshed_at` is the per-listing cooldown marker the sweep stamps on enqueue,
-- so a listing isn't re-fetched again until the cooldown elapses (bounds re-fetch load
-- and stops a genuinely-removed photo from looping).

alter table listings add column if not exists images_refreshed_at timestamptz;

-- Supports the sweep's "does this listing have an un-downloaded image?" EXISTS probe
-- (and the source_unavailable refinement) without scanning the whole images table.
create index if not exists images_unstored_by_listing_idx
  on images (sreality_id)
  where storage_path is null;
