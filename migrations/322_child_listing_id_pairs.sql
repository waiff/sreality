-- 322_child_listing_id_pairs.sql
-- R2 Phase A1 of the listing-identity refactor, file 3 of 6
-- (docs/design/listing-identity-r2-pk-swap-runbook.md § 2, § 0.5).
-- Additive: listing_id_a / listing_id_b on the four pair caches, and
-- left/right_listing_id on the dedup_pair_audit ledger.
--
-- DELIBERATELY NOT re-canonicalized. The legacy columns carry
-- CHECK (sreality_id_a < sreality_id_b) + UNIQUE(a, b, <discriminator>), and under
-- the new surrogate that ordering flips for every mixed sreality/synthetic pair
-- (68.5% of inventory is negative-id today). Physically swapping a/b would have to
-- move the SIDE-COUPLED PAYLOADS in lockstep — toolkit/visual_match.py and
-- toolkit/image_similarity.py both store side-ordered image lists keyed to the
-- sorted pair — which is a silent-corruption risk for no benefit.
--
-- Instead, Phase B adds an ORDER-INDEPENDENT functional unique index per cache:
--   UNIQUE (LEAST(listing_id_a, listing_id_b), GREATEST(listing_id_a, listing_id_b), <disc>)
-- It dedupes existing positional rows and post-flip NULL-sreality_id pairs alike,
-- needs no swap, and leaves the legacy CHECK/UNIQUE frozen-valid until R5. The
-- consequence, which new code MUST respect: listing_id_a < listing_id_b is NOT an
-- invariant on these tables. Read them order-independently.
--
-- dedup_pair_audit has no unique constraint on its pair columns (append-only,
-- pkey on id), so it needs a reader repoint only — no guard to replace.
--
-- Catalog-only, short lock_timeout, no FK yet — see 320's header for the rationale.

SET lock_timeout = '3s';

ALTER TABLE listing_image_comparisons
    ADD COLUMN IF NOT EXISTS listing_id_a bigint,
    ADD COLUMN IF NOT EXISTS listing_id_b bigint;

ALTER TABLE listing_visual_matches
    ADD COLUMN IF NOT EXISTS listing_id_a bigint,
    ADD COLUMN IF NOT EXISTS listing_id_b bigint;

ALTER TABLE listing_floor_plan_matches
    ADD COLUMN IF NOT EXISTS listing_id_a bigint,
    ADD COLUMN IF NOT EXISTS listing_id_b bigint;

ALTER TABLE listing_site_plan_matches
    ADD COLUMN IF NOT EXISTS listing_id_a bigint,
    ADD COLUMN IF NOT EXISTS listing_id_b bigint;

ALTER TABLE dedup_pair_audit
    ADD COLUMN IF NOT EXISTS left_listing_id bigint,
    ADD COLUMN IF NOT EXISTS right_listing_id bigint;
