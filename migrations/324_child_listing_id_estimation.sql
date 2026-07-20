-- 324_child_listing_id_estimation.sql
-- R2 Phase A1 of the listing-identity refactor, file 5 of 6
-- (docs/design/listing-identity-r2-pk-swap-runbook.md § 2).
-- Additive: the surrogate on the estimation family.
--
-- estimation_runs.input_listing_id is target-design item 6, never previously
-- shipped (Phase 0 only made the existing input_sreality_id match source-agnostic
-- and demoted the fragile input_url arm to an explicit fallback). Once the portal
-- lookup collapses onto it in Phase C, a post-flip non-sreality estimation stops
-- degrading to URL string-equality matching.
--
-- estimation_cohort_entries carries sreality_id inside its composite PK
-- (estimation_run_id, sreality_id), so it cannot simply be relaxed later — Phase D
-- swaps that PK to (estimation_run_id, listing_id). The column lands here.
--
-- Frozen JSONB payloads (comparables_used / comparables_excluded / input_spec /
-- trace) are deliberately NOT touched: rule 8 keeps them immutable, and legacy
-- sreality_ids inside them stay resolvable forever because existing values are
-- never NULLed. Phase C extends the FORWARD path only, so new estimations record
-- comparable provenance that survives the flip.
--
-- Catalog-only, short lock_timeout, no FK yet — see 320's header for the rationale.

SET lock_timeout = '3s';

ALTER TABLE estimation_runs ADD COLUMN IF NOT EXISTS input_listing_id bigint;
ALTER TABLE building_runs ADD COLUMN IF NOT EXISTS input_listing_id bigint;
ALTER TABLE estimation_cohort_entries ADD COLUMN IF NOT EXISTS listing_id bigint;
