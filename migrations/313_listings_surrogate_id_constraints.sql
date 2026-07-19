-- 313_listings_surrogate_id_constraints.sql
-- R1 of the listing-identity refactor, SECOND of two files (312 added the column).
-- Promotes listings.id to UNIQUE + present-enforced, so R2 child tables can FK to
-- it. The PK stays on sreality_id until the R4 cutover.
--
-- This file is the FRESH-REBUILD form (empty table => every step instant). On the
-- LIVE database these same end-states were reached online, out-of-band, in this
-- order (see scripts/backfill_listing_surrogate_id.py + the R1 PR notes):
--   1. backfill listings.id for all pre-existing rows (chronological, batched,
--      FOR UPDATE SKIP LOCKED — never deadlocks the always-on writer);
--   2. CREATE UNIQUE INDEX CONCURRENTLY listings_id_uidx (non-blocking build);
--   3. the ADD CONSTRAINT ... USING INDEX + CHECK/VALIDATE below, each under a
--      short lock_timeout with retry so the brief catalog lock never parks at the
--      head of listings' lock queue.
-- A validated CHECK (id IS NOT NULL) is used instead of a column SET NOT NULL:
-- same integrity guarantee, and the true column-NOT-NULL comes for free when id
-- becomes the PRIMARY KEY at R4 (at which point this CHECK is dropped).

SET lock_timeout = '8s';

CREATE UNIQUE INDEX IF NOT EXISTS listings_id_uidx ON listings (id);

ALTER TABLE listings
    ADD CONSTRAINT listings_id_key UNIQUE USING INDEX listings_id_uidx;

ALTER TABLE listings
    ADD CONSTRAINT listings_id_present_check CHECK (id IS NOT NULL) NOT VALID;
ALTER TABLE listings VALIDATE CONSTRAINT listings_id_present_check;
