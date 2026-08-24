-- W4 (hydration sprint): the board's cover-photo read asked for ONE thumbnail
-- per card (`fetchImagesForListingIds(ids, 1)`) but images_public has no
-- per-listing LIMIT, so PostgREST returned every image row for every listing
-- in scope and the client discarded all but the first. Worse, images_public
-- LEFT JOINs LATERAL to image_clip_tags per row for the tag badge, so the
-- server paid a correlated CLIP-tag lookup for every discarded row too.
-- Measured live (44 real listing ids, images_public): 901 image rows, 901
-- CLIP-tag lateral probes, 3,995 buffers, 380ms.
--
-- listing_cover_public computes the ONE cover row per listing FIRST (a
-- DISTINCT ON over the existing images_listing_id_sequence_key index — no new
-- index needed, it already provides listing_id/sequence in presorted order,
-- so Postgres does an Incremental Sort instead of a full one), THEN joins the
-- CLIP lateral only to that already-reduced set. Measured live (same 44 ids):
-- 44 rows, 44 CLIP-tag probes, 788 buffers, 59ms — a 5x buffer cut and the
-- lateral probe count now equals the row count instead of ~20x it.
-- No security_invoker (matches images_public, its sibling/analog) — this
-- view family is shared market data, not per-account RLS-scoped, so access
-- control lives entirely in the grants below, not row-level policy pass-through.
create or replace view listing_cover_public as
with cover as (
  select distinct on (listing_id)
    id, listing_id, sreality_id, sequence, sreality_url, storage_path, phash
  from images
  where listing_id is not null
  order by listing_id, sequence nulls last, id
)
select
  c.id,
  c.sreality_id,
  c.sequence,
  c.sreality_url,
  c.storage_path,
  ct.fine_tag as clip_fine_tag,
  ct.logical_tag as clip_logical_tag,
  ct.confidence as clip_confidence,
  ct.render_score as clip_render_score,
  c.phash,
  c.listing_id
from cover c
left join lateral (
  select t.fine_tag, t.logical_tag, t.confidence, t.render_score
  from image_clip_tags t
  where t.image_id = c.id
  order by t.tagged_at desc
  limit 1
) ct on true;

revoke all on listing_cover_public from public, anon;
grant select on listing_cover_public to authenticated;
