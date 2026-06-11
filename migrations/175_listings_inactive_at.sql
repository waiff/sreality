-- 175_listings_inactive_at.sql
--
-- Stamp WHEN a listing flipped to is_active=false. Until now the flip was a
-- bare boolean — delisting latency (how long a gone listing stayed nominally
-- active before we noticed) was unmeasurable, because nothing recorded the
-- flip moment. Every write site that sets is_active=false now also sets
-- inactive_at = now(); every reactivation (touch_listings' react CTE, the
-- upsert ON CONFLICT paths) clears it back to NULL. Historical rows stay NULL
-- — the delisting-latency health check (migration 176) ignores NULLs, so the
-- metric simply starts accruing from the day this lands.

alter table listings add column if not exists inactive_at timestamptz;

comment on column listings.inactive_at is
  'When is_active last flipped to false (cleared on reactivation). NULL on '
  'rows that have never flipped since migration 175 — the delisting-latency '
  'health check ignores NULLs.';

-- The health check scans "flipped in the last 7 days" on every Health-page
-- poll. Partial index keeps it off the (mostly-NULL) bulk of the table.
create index if not exists listings_inactive_at_idx
  on listings (inactive_at)
  where inactive_at is not null;
