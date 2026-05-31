-- Reconcile schema drift: listings.street / house_number / zip / street_id
-- exist in production but were never captured in a tracked migration (applied
-- out-of-band). This forward migration makes a fresh rebuild match prod; it is
-- a no-op against the live DB (IF NOT EXISTS). The columns are currently
-- unpopulated (0% across all sources) — parsing/backfilling structured
-- addresses is a separate effort; this only fixes the migration source-of-truth.
ALTER TABLE listings ADD COLUMN IF NOT EXISTS street       text;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS house_number text;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS zip          text;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS street_id    integer;
