-- 321_child_listing_id_caches.sql
-- R2 Phase A1 of the listing-identity refactor, file 2 of 6
-- (docs/design/listing-identity-r2-pk-swap-runbook.md § 2).
-- Additive: listing_id on the per-listing analytical / enrichment caches. All are
-- keyed (sreality_id, snapshot_id[, model]) today; the matching listing_id-based
-- unique guards arrive in Phase B, and the writers' ON CONFLICT targets move onto
-- them in Phase C — never before the guard exists, or the arbiter miss wedges the
-- writer (the #825 failure class).
--
-- Catalog-only, short lock_timeout, no FK yet — see 320's header for the rationale.

SET lock_timeout = '3s';

ALTER TABLE listing_condition_scores ADD COLUMN IF NOT EXISTS listing_id bigint;
ALTER TABLE listing_marker_extractions ADD COLUMN IF NOT EXISTS listing_id bigint;
ALTER TABLE listing_summaries ADD COLUMN IF NOT EXISTS listing_id bigint;
ALTER TABLE building_unit_extractions ADD COLUMN IF NOT EXISTS listing_id bigint;
ALTER TABLE listing_description_enrichments ADD COLUMN IF NOT EXISTS listing_id bigint;
