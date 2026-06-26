-- 240_image_clip_render_null_idx.sql
-- Partial index so the render_score backfill (scripts/backfill_render_score.py) finds the
-- not-yet-scored rows in an index scan instead of a seq scan over 2M+ tags, and a scored
-- row drops straight out — the same marker-partial-index pattern as images.clip_tagged_at.
-- Self-empties as the backfill completes (the index covers only render_score IS NULL).
create index if not exists image_clip_tags_render_null_idx
  on image_clip_tags (image_id) where render_score is null;
