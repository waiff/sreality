-- Render labeling: drawings/documents are not photos, so the render-vs-photo axis is noise
-- for them (its anchors are about interiors). Stop applying render_score to plan/document
-- tags and add a `property_document` logical tag (energy certificates, contracts, spec tables).
--
-- (1) Allow the new logical tag on the Haiku room-classifier cache CHECK (forward-consistency;
--     image_clip_tags.logical_tag is free-text, so CLIP can already write it). Additive.
alter table image_room_classifications drop constraint if exists image_room_classifications_room_type_check;
alter table image_room_classifications add constraint image_room_classifications_room_type_check
  check (room_type = any (array[
    'kitchen','bathroom','toilet','living_room','bedroom','hallway',
    'exterior_facade','balcony_terrace','garden',
    'floor_plan','site_plan','property_document','other']));

-- (2) NULL the meaningless render_score on every existing plan/document image. The CLIP
--     tagger leaves it NULL going forward (scraper/clip_tagger), and the render-score
--     backfill skips these tags, so they stay NULL. The UI render badge self-hides on a NULL
--     score, so "RENDER 99" disappears from floor plans. No functional impact — plan/document
--     tags are already excluded from the byt pHash/cosine merge signal (NON_INTERIOR_TAGS).
update image_clip_tags set render_score = null
where logical_tag in ('floor_plan', 'site_plan', 'property_document') and render_score is not null;

-- NOTE: populating `property_document` on EXISTING images requires a CLIP re-tag (the tagger
-- learns the new anchors). New/re-tagged images pick it up automatically; a full re-tag is an
-- operational follow-up (reset images.clip_tagged_at as desired).
