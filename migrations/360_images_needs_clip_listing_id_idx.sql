-- 359_images_needs_clip_listing_id_idx.sql
--
-- clip_tag_backfill.py's region arm (scripts/clip_tag_backfill.py::_SELECT_REGION) drove
-- from a kraj's listings into its untagged images via images_needs_clip_sid_idx
-- (migration 232), keyed on sreality_id. Post-Gate-2 that column goes NULL for every
-- non-sreality row, and the region arm's join was switched (same PR) from
-- `images.sreality_id = listings.sreality_id` to the surrogate `images.listing_id =
-- listings.id` — images.listing_id is fully populated + NOT-NULL-enforced (migration
-- 350), so it's always joinable. This index gives that new join path the same
-- index-only, shrinks-as-coverage-grows plan the old sid index gave the sreality-only
-- join; the sid index is left in place (still exploited by legacy sreality_id-keyed
-- reads elsewhere) rather than dropped.
create index if not exists images_needs_clip_lid_idx
  on images (listing_id)
  where clip_tagged_at is null and storage_path is not null;
