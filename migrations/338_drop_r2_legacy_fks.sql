-- 338_drop_r2_legacy_fks.sql
-- R2 Phase D step 6 (docs/design/listing-identity-r2-pk-swap-runbook.md §5 item 6):
-- drop the 19 legacy child FKs onto listings(sreality_id). Integrity is already
-- held by the parallel listing_id -> listings(id) FKs Phase B validated (PRs
-- #837/#838). Not gate-destructive: re-addable any time (NOT VALID -> VALIDATE).
-- Applied live via scripts/drop_r2_legacy_fks.py (each DROP is its own retried
-- transaction against the always-on writer); this is the plain form for fresh
-- rebuilds.

ALTER TABLE building_unit_extractions DROP CONSTRAINT IF EXISTS building_unit_extractions_sreality_id_fkey;
ALTER TABLE dirty_broker_listings DROP CONSTRAINT IF EXISTS dirty_broker_listings_sreality_id_fkey;
ALTER TABLE images DROP CONSTRAINT IF EXISTS images_sreality_id_fkey;
ALTER TABLE listing_condition_scores DROP CONSTRAINT IF EXISTS listing_condition_scores_sreality_id_fkey;
ALTER TABLE listing_floor_plan_matches DROP CONSTRAINT IF EXISTS listing_floor_plan_matches_sreality_id_a_fkey;
ALTER TABLE listing_floor_plan_matches DROP CONSTRAINT IF EXISTS listing_floor_plan_matches_sreality_id_b_fkey;
ALTER TABLE listing_image_comparisons DROP CONSTRAINT IF EXISTS listing_image_comparisons_sreality_id_a_fkey;
ALTER TABLE listing_image_comparisons DROP CONSTRAINT IF EXISTS listing_image_comparisons_sreality_id_b_fkey;
ALTER TABLE listing_marker_extractions DROP CONSTRAINT IF EXISTS listing_marker_extractions_sreality_id_fkey;
ALTER TABLE listing_site_plan_matches DROP CONSTRAINT IF EXISTS listing_site_plan_matches_sreality_id_a_fkey;
ALTER TABLE listing_site_plan_matches DROP CONSTRAINT IF EXISTS listing_site_plan_matches_sreality_id_b_fkey;
ALTER TABLE listing_snapshots DROP CONSTRAINT IF EXISTS listing_snapshots_sreality_id_fkey;
ALTER TABLE listing_summaries DROP CONSTRAINT IF EXISTS listing_summaries_sreality_id_fkey;
ALTER TABLE listing_videos DROP CONSTRAINT IF EXISTS listing_videos_sreality_id_fkey;
ALTER TABLE listing_visual_matches DROP CONSTRAINT IF EXISTS listing_visual_matches_sreality_id_a_fkey;
ALTER TABLE listing_visual_matches DROP CONSTRAINT IF EXISTS listing_visual_matches_sreality_id_b_fkey;
ALTER TABLE manual_rental_estimates DROP CONSTRAINT IF EXISTS manual_rental_estimates_sreality_id_fkey;
ALTER TABLE properties DROP CONSTRAINT IF EXISTS properties_repr_listing_id_fkey;
ALTER TABLE property_notes DROP CONSTRAINT IF EXISTS property_notes_origin_listing_id_fkey;
