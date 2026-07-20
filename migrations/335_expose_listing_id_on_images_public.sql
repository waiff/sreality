-- 335_expose_listing_id_on_images_public.sql
-- R2 Phase C read cutover: images_public needs listing_id exposed so the
-- ListingDetail resolver-chain cutover (this PR) can filter images by the
-- surrogate instead of sreality_id. Purely additive — a new trailing column,
-- no existing column renamed/reordered/dropped. listing_freshness_checks is
-- NOT touched: it has no listing_id column at all (rule #9 — append-only
-- observability, not an R2 carrier), so its _public view correctly stays
-- sreality_id-keyed forever.

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
    i.listing_id
   FROM images i
     LEFT JOIN LATERAL ( SELECT t.fine_tag,
            t.logical_tag,
            t.confidence,
            t.render_score
           FROM image_clip_tags t
          WHERE t.image_id = i.id
          ORDER BY t.tagged_at DESC
         LIMIT 1) ct ON true;
