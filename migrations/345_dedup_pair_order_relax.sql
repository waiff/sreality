-- 345_dedup_pair_order_relax.sql
-- R2 dedup-identity-chain PR1 of 4 (docs/design/listing-identity-r2-pk-swap-runbook.md
-- §4 item 1 / memory r2-read-cutover-2026-07-21). Two independent, additive changes:
--
-- 1. Drop the legacy `CHECK (sreality_id_a < sreality_id_b)` on the four dedup pair
--    caches. Phase A (migration 322) already added listing_id_a/listing_id_b to all
--    four and Phase B/B2 (apply_r2_constraints.py / apply_r2_unique_guards.py — no
--    migration file, see the runbook's ledger-divergence note) already built the
--    order-independent functional unique index `(LEAST(listing_id_a, listing_id_b),
--    GREATEST(...), <discriminator>)` plus NOT-NULL-style presence CHECKs on both
--    surrogate columns. The legacy ordering CHECK is now the ONLY thing left that
--    still assumes `sreality_id_a < sreality_id_b`, which is untrue for 77% of pairs
--    once sorted by the surrogate — it must go before any writer can insert or
--    update a row ordered by listing_id. Dropping it is pure relaxation: nothing
--    currently reads or writes these tables out of legacy order, so no code changes
--    ride with this migration.
-- 2. Add nullable listing_id_a/listing_id_b to dedup_batch_requests (the Anthropic
--    Batch API spool, migration 197/306) — not an existing R2 carrier (the batch
--    request/response cycle can span 24h, so PR2 handles the writer dual-write +
--    custom_id scheme change and any backfill together, not this schema-only step).
--
-- Both are small, low-traffic tables — no lock_timeout retry loop needed, but keep
-- the short timeout out of habit for anything that takes ACCESS EXCLUSIVE.

SET lock_timeout = '3s';

ALTER TABLE listing_image_comparisons
    DROP CONSTRAINT IF EXISTS listing_image_comparisons_check;

ALTER TABLE listing_visual_matches
    DROP CONSTRAINT IF EXISTS listing_visual_matches_check;

ALTER TABLE listing_floor_plan_matches
    DROP CONSTRAINT IF EXISTS listing_floor_plan_matches_check;

ALTER TABLE listing_site_plan_matches
    DROP CONSTRAINT IF EXISTS listing_site_plan_matches_check;

ALTER TABLE dedup_batch_requests
    ADD COLUMN IF NOT EXISTS listing_id_a bigint,
    ADD COLUMN IF NOT EXISTS listing_id_b bigint;
