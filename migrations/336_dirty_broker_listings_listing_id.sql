-- 336_dirty_broker_listings_listing_id.sql
-- R2 Phase D step 2: nullable listing_id dual-write column on dirty_broker_listings.
-- This table was deliberately left out of R2_CARRIERS (toolkit/listing_identity.py)
-- because its lifecycle is a queue (claimed and deleted, never read as history) —
-- so instead of the big-carrier backfill workflow, it gets a small direct one once
-- this column + the matching writer-code dual-write (scraper/db.py) are live.
-- Additive only: nullable, no NOT NULL / PK swap here (the #825 lesson — enforcing
-- before the writer deploy is live would break the ingest/batch-drain writers that
-- still only know sreality_id at migration time). The PK swap to (listing_id) is a
-- follow-up migration once this deploy is confirmed live (see
-- docs/design/listing-identity-r2-pk-swap-runbook.md Progress section).

ALTER TABLE dirty_broker_listings ADD COLUMN IF NOT EXISTS listing_id bigint
  REFERENCES listings (id);

CREATE INDEX IF NOT EXISTS dirty_broker_listings_listing_id_idx
  ON dirty_broker_listings (listing_id);
