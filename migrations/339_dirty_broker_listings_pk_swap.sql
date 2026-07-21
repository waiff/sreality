-- 339_dirty_broker_listings_pk_swap.sql
-- R2 Phase D step 2 (dirty_broker_listings sub-step, finalized): swap this table's
-- PK from sreality_id to listing_id. migration 336 shipped the nullable dual-write
-- column + writer code; this was deliberately deferred until the writer deploy was
-- confirmed fully live, per the #825 lesson (a constraint added before every writer
-- honors it breaks the still-lagging ones). Verified live before writing this:
-- 6+ hours post-336-merge, zero rows in dirty_broker_listings have a NULL
-- listing_id (checked across the whole table, not just recent rows) — the fleet,
-- including the GH-Actions-cron portals subject to the SHA-freeze gotcha, has
-- fully rolled over. This migration must land BEFORE the matching writer-code PR
-- (retargets both ON CONFLICT sites to listing_id) deploys, or the new code's
-- ON CONFLICT (listing_id) has no unique index to infer from.

-- Defensive: the live check found zero, but a straggler between that check and
-- this migration applying is possible on a table this actively written.
UPDATE dirty_broker_listings d SET listing_id = l.id
FROM listings l WHERE l.sreality_id = d.sreality_id AND d.listing_id IS NULL;

ALTER TABLE dirty_broker_listings ALTER COLUMN listing_id SET NOT NULL;

-- A fresh dedicated index, not a promotion of migration 336's plain
-- dirty_broker_listings_listing_id_idx — CREATE UNIQUE INDEX can't convert an
-- existing plain index in place, so build unique first, then drop the redundant
-- plain one (a unique index already satisfies every plain-index use).
CREATE UNIQUE INDEX IF NOT EXISTS dirty_broker_listings_listing_id_key
  ON dirty_broker_listings (listing_id);
DROP INDEX IF EXISTS dirty_broker_listings_listing_id_idx;

ALTER TABLE dirty_broker_listings DROP CONSTRAINT IF EXISTS dirty_broker_listings_pkey;
ALTER TABLE dirty_broker_listings
  ADD CONSTRAINT dirty_broker_listings_pkey
  PRIMARY KEY USING INDEX dirty_broker_listings_listing_id_key;

-- No longer PK-enforced; relax for symmetry with the rest of Phase D (prep for a
-- future Gate-2 row with no sreality_id at all). This table's legacy FK was
-- already dropped in migration 338 (drop_r2_legacy_fks.py, 2026-07-20).
ALTER TABLE dirty_broker_listings ALTER COLUMN sreality_id DROP NOT NULL;
