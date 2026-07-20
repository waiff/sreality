-- 312_listings_surrogate_id.sql
-- R1 of the listing-identity refactor (docs/design/listing-identity-refactor.md).
-- Additive: introduces the clean surrogate listings.id. FIRST of two files — 313
-- promotes it to UNIQUE NOT NULL after the backfill. Every statement here is
-- instant / metadata-only, safe on the live 556k-row table with the always-on
-- writer (a short lock_timeout keeps the brief catalog lock from parking at the
-- head of listings' lock queue).
--
-- The surrogate is SEQUENCE-backed for now, NOT yet GENERATED ALWAYS AS IDENTITY
-- — that conversion is deferred to the R4 cutover (converting requires DROP
-- DEFAULT + ADD IDENTITY + advancing the owned sequence past the backfill max, so
-- it belongs at the very end). New rows get a value immediately; existing rows are
-- backfilled by scripts/backfill_listing_surrogate_id.py in first_seen_at order,
-- so ascending id tracks market chronology (useful for keyset pagination + a
-- meaningful cross-portal "sort by id" later).
--
-- Sequence starts at 10,000,000 — far above the ~556k backfilled ids — so the two
-- epochs never collide, legacy(<1M) vs post-cutover(>=10M) rows stay visually
-- distinct, and the id space stays globally monotonic (older row => smaller id,
-- because every legacy row predates every post-cutover insert).

SET lock_timeout = '5s';

CREATE SEQUENCE IF NOT EXISTS listings_id_seq START WITH 10000000;

-- ADD COLUMN with a VOLATILE default (nextval) would force a full table rewrite
-- under ACCESS EXCLUSIVE; splitting it keeps both steps metadata-only. New rows
-- get a value from SET DEFAULT onward; existing rows stay NULL until the backfill.
ALTER TABLE listings ADD COLUMN IF NOT EXISTS id bigint;
ALTER TABLE listings ALTER COLUMN id SET DEFAULT nextval('listings_id_seq');
ALTER SEQUENCE listings_id_seq OWNED BY listings.id;

COMMENT ON COLUMN listings.id IS
    'Clean surrogate key (R1, migration 312). Sequence-backed for now; becomes the '
    'PK + GENERATED ALWAYS AS IDENTITY at the R4 cutover. sreality_id stays the PK '
    'and the sreality natural-key mirror until then.';
