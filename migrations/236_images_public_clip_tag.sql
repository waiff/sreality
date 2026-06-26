-- 234_images_public_clip_tag.sql
--
-- Expose the CLIP per-image tag (image_clip_tags, migration 225) to the anon
-- browser through the EXISTING images_public boundary, so every image-rendering
-- surface (Browse cards, Listing Detail, /dedup, map hovers, comparables) reads
-- the tag from the SAME row it already reads the image from — one source,
-- app-wide, no second fetch path. CLIP is the single per-image content tag we
-- expose: it is free, full-inventory (the only tagger that covers
-- dum/pozemek/komercni at all), and the production dedup engine already prefers
-- it (app_settings.dedup_prefer_clip_tags = true).
--
-- We expose all three CLIP columns; the frontend displays fine_tag (the raw
-- winning anchor — keeps the plot-identity distinctions cadastral/aerial/
-- situation that the logical collapse hides) and shows confidence in a tooltip.
--
-- Latest-model-wins lateral: today there is exactly ONE model
-- (openai/clip-vit-base-patch32). `LEFT JOIN LATERAL (... ORDER BY tagged_at
-- DESC LIMIT 1)` returns exactly one row per image and deterministically picks
-- the newest if a second model is ever added — WITHOUT a schema change and
-- without ever duplicating an image row (a bare JOIN ON (image_id) would, the
-- moment a 2nd model lands, silently break the gallery's one-row-per-image
-- contract). The new columns are NULL for not-yet-tagged images (~73% today,
-- ramping down): the frontend treats NULL as "untagged" and shows no badge.
--
-- images_public is a non-filtering passthrough of images, so CREATE OR REPLACE
-- only APPENDS the three clip_* columns (allowed; existing columns + order
-- unchanged). The view's existing anon SELECT grant covers the new columns.

create or replace view images_public as
select
  i.id,
  i.sreality_id,
  i.sequence,
  i.sreality_url,
  i.storage_path,
  ct.fine_tag    as clip_fine_tag,
  ct.logical_tag as clip_logical_tag,
  ct.confidence  as clip_confidence
from images i
left join lateral (
  select t.fine_tag, t.logical_tag, t.confidence
  from image_clip_tags t
  where t.image_id = i.id
  order by t.tagged_at desc
  limit 1
) ct on true;

-- The ONLY full-table-scan consumer of images_public is the Health "Image
-- mirror" matview (migration 115). Repoint it at the BASE images table so the
-- new per-row lateral can never execute during its REFRESH (off-request,
-- 2-hourly via scripts/refresh_image_stats.py / images.yml). This is
-- semantically identical: images_public is `select <cols> from images` with no
-- WHERE and no RLS predicate on the view itself, so count(id)/count(storage_path)
-- are byte-for-byte the same against the base table — and it fully decouples the
-- dashboard matview from any future shape of images_public.
--
-- Safe to drop+recreate: the dependency audit shows nothing depends on this
-- matview (image_storage_overview() reads it by name as an old-style SQL
-- function, which is not a tracked dependency, so the DROP is not blocked and
-- the function resolves the recreated matview).
drop materialized view if exists image_storage_overview_mv;
create materialized view image_storage_overview_mv as
  select
    l.category_main,
    l.category_type,
    count(i.id)                                       as total,
    count(i.storage_path)                             as stored,
    count(i.id) filter (where l.is_active)            as total_active,
    count(i.storage_path) filter (where l.is_active)  as stored_active
  from listings_public l
  left join images i on i.sreality_id = l.sreality_id
  group by 1, 2;

-- Required for REFRESH MATERIALIZED VIEW CONCURRENTLY (scripts/refresh_image_stats.py).
create unique index if not exists image_storage_overview_mv_cat
  on image_storage_overview_mv (category_main, category_type);

grant select on image_storage_overview_mv to anon;
