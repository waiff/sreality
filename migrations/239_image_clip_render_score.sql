-- 239_image_clip_render_score.sql
-- Render-vs-photo score per image (the validated #3 render detector), stored on the
-- CLIP tag row and exposed to the anon browser via the EXISTING images_public boundary.
--
-- render_score = CLIP softmax over render-vs-photo text anchors (data/clip_taxonomy.json),
-- 0..1, an orthogonal axis to the room argmax (a render IS a kitchen-render). The dedup
-- engine excludes high-render images from the byt pHash/cosine merge signal (a development
-- reuses RENDERS across distinct units — the residual the area + room-type gates can't
-- catch); the listing-detail gallery shows the score so the operator can eyeball-validate.
-- Validated: Na Bradle renders 0.55-0.99, bazos amateur-photo control 0.05-0.20.

-- 1. The column on the CLIP tag row (NULL until the tagger backfills it, like the other
--    clip_* columns; the backfill writes it in the same upsert).
alter table image_clip_tags add column if not exists render_score real;

-- 2. Append clip_render_score to images_public (CREATE OR REPLACE only adds a trailing
--    column — existing columns + order unchanged; the view's anon SELECT grant covers it).
--    Same latest-model-wins lateral as migration 236.
create or replace view images_public as
select
  i.id,
  i.sreality_id,
  i.sequence,
  i.sreality_url,
  i.storage_path,
  ct.fine_tag      as clip_fine_tag,
  ct.logical_tag   as clip_logical_tag,
  ct.confidence    as clip_confidence,
  ct.render_score  as clip_render_score
from images i
left join lateral (
  select t.fine_tag, t.logical_tag, t.confidence, t.render_score
  from image_clip_tags t
  where t.image_id = i.id
  order by t.tagged_at desc
  limit 1
) ct on true;
