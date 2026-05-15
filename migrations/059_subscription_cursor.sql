-- Per-subscription cursor for the Watchdog matcher.
--
-- Migration 058 seeded a single global watermark in app_settings.
-- That had two flaws:
--   1. It was seeded with now() at migration time. The most recent
--      scrape had already finished and every listing.first_seen_at was
--      older than the seed, so the matcher saw nothing forever.
--   2. A single global cursor coupled every subscription together —
--      a fresh watchdog created today could never fire on listings
--      ingested before its own creation, even if those listings were
--      newer than the cursor was when a different watchdog last
--      advanced it.
--
-- Replace it with a per-subscription cursor. The matcher walks each
-- subscription independently, considering listings whose first_seen_at
-- > cursor and advancing the cursor to the max first_seen_at of the
-- evaluated window. Default now() - 24h on insert means a freshly
-- created watchdog fires on the past 24 hours of matching listings
-- as its initial backfill — instant feedback without waiting for the
-- next nightly scrape.
--
-- The global watermark row in app_settings stays (we don't DELETE
-- it here) so older code paths reading it during a partial deploy
-- keep their "nothing to do" behaviour. The new matcher ignores it.

ALTER TABLE notification_subscriptions
  ADD COLUMN last_matched_first_seen_at timestamptz NOT NULL
    DEFAULT now() - interval '24 hours';

COMMENT ON COLUMN notification_subscriptions.last_matched_first_seen_at IS
  'Cursor for the matcher (replaces the migration-058 global watermark).
   The matcher evaluates listings with first_seen_at > cursor for this
   subscription only, advancing the cursor to the max first_seen_at of
   the evaluated window each pass. Default now() - 24h on insert means
   a fresh watchdog backfills the past 24 hours, giving the operator
   immediate feedback.';

-- Backfill existing rows so they fire on the 24 hours prior to their
-- creation, capped at the past 7 days for safety against ancient
-- watchdogs that would otherwise flood the feed with months of
-- listings on the first matcher pass after deploy.
UPDATE notification_subscriptions
SET last_matched_first_seen_at = GREATEST(
  created_at - interval '24 hours',
  now() - interval '7 days'
);

-- Index isn't required: the matcher always filters by subscription id
-- first (small N of active rows). The cursor is read alongside the
-- existing per-row scan.
