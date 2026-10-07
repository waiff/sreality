-- 591: images_public carries the ACTIVE tag model's per-head scores.
--
-- Browse's "Cover" dropdown picks the photo every card opens on. Operator ruling
-- 2026-10-07: the trained DINOv3 heads decide wherever they have scored a photo,
-- CLIP only where they have not — and that must keep holding as more photos are
-- scored. So this reads the ACTIVE model's row live (tag_head_models allows one),
-- never a snapshot: a photo scored tomorrow, or a model activated tomorrow,
-- reaches Browse with no change here.
--
-- This narrows migration 490's "no `_public` view and none is planned" for the
-- SCORES ONLY: the three tag-model tables stay RLS-locked and service-role-only,
-- and this owner-rights view is the one read path, exactly as image_clip_tags is
-- published (migrations 236/237). A score is a market fact about a photo.
--
-- A plain LEFT JOIN on the (image_id, model_id) primary key, not a lateral, so
-- the planner removes the join for every reader that does not select
-- tag_head_scores (measured live: the InitPlan is never executed). Only the
-- Browse card photo read pays: one index probe per photo, ~4 buffers.
--
-- tag_head_scores = {"<tag_taxonomy.id>": probability 0..1, ...}, one independent
-- sigmoid per head of the active model. NULL when the active model has not scored
-- the photo, or no model is active.
--
-- Migration 496's body byte-for-byte, with tag_head_scores appended as the LAST
-- column so every existing ordinal is untouched.
set local lock_timeout = '5s';

-- ci-allow-ungated: images_public — reads image_tag_scores + tag_head_models on
-- purpose and WITHOUT an admin gate: Browse cards are for every signed-in user,
-- and it publishes only a photo's scores and which model is active (no account,
-- no PII, no labels). Same posture as its CLIP tag columns (migration 236).

CREATE OR REPLACE VIEW images_public AS
 SELECT i.id,
    i.sreality_id,
    i.sequence,
    i.sreality_url,
    i.storage_path,
    ct.fine_tag AS clip_fine_tag,
    ct.logical_tag AS clip_logical_tag,
    ct.confidence AS clip_confidence,
    ct.render_score AS clip_render_score,
    i.phash,
    i.listing_id,
    i.rendition,
    th.scores AS tag_head_scores
   FROM images i
     LEFT JOIN LATERAL ( SELECT t.fine_tag,
            t.logical_tag,
            t.confidence,
            t.render_score
           FROM image_clip_tags t
          WHERE t.image_id = i.id
          ORDER BY t.tagged_at DESC
         LIMIT 1) ct ON true
     LEFT JOIN image_tag_scores th
       ON th.image_id = i.id
      AND th.model_id = (SELECT m.id FROM tag_head_models m WHERE m.status = 'active');

COMMENT ON COLUMN images_public.tag_head_scores IS
  'The ACTIVE tag model''s per-head scores for this photo, {"<tag_taxonomy.id>": 0..1} (independent sigmoids, image_tag_scores.scores). NULL = not scored by the active model. Read by the Browse card cover choice (migration 591).';
