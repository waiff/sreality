-- 325_listings_condition_propagated_from_id.sql
-- R2 Phase A1 of the listing-identity refactor, file 6 of 6
-- (docs/design/listing-identity-r2-pk-swap-runbook.md § 0.11, § 2).
-- Additive: the surrogate twin of listings.condition_levels_propagated_from.
--
-- This is the carrier BOTH earlier censuses missed. It is a listing id living
-- INSIDE listings itself (migration 174: "which sibling listing did these condition
-- levels come from"), so it is invisible to an FK-graph walk AND to a scan of other
-- tables' columns. toolkit/condition_scoring.py stamps it from s.sreality_id and
-- scripts/backfill_condition_scores.py reads it back for the sibling-heal.
--
-- Dormant today — condition scoring is intentionally paused — but latent: the
-- moment scoring resumes on a post-flip row whose sreality_id is NULL, the stamp
-- silently records nothing and the sibling-heal mis-selects. Repointing must
-- therefore land before scoring is re-enabled, not before the flip only.
--
-- listings is the hottest table in the schema, so this ALTER gets its own file and
-- its own transaction: nothing else should be queued behind it.

SET lock_timeout = '3s';

ALTER TABLE listings ADD COLUMN IF NOT EXISTS condition_levels_propagated_from_id bigint;
