-- 006_freshness_checks.sql
-- Log every on-demand freshness check the toolkit/agent triggers via
-- verify_listing_freshness. Two purposes:
--   1) Observability — find out which listings are being verified
--      most often, detect runaway agent loops, audit API usage.
--   2) Throttle support — used by verify_listing_freshness to coalesce
--      repeated checks of the same listing within a short window. The
--      toolkit returns cached data instead of re-scraping if a check
--      happened within max_age_hours.
--
-- This is also the "I confirmed this listing exists at time T" signal,
-- separate from listings.last_seen_at. last_seen_at remains driven by
-- the cron index walk (architectural rule #4); a freshness check does
-- NOT bump it. The toolkit uses GREATEST(last_seen_at, latest
-- checked_at here) when computing effective freshness.
--
-- Outcome values:
--   'unchanged'      - listing exists, content_hash matched
--   'updated'        - listing exists, content_hash changed (new snapshot written)
--   'gone'           - listing returned 404 or 410
--   'fetch_error'    - HTTP/parse/DB error (details in error_message)
--
-- Cleanup: rows older than 30 days can be safely deleted. No automated
-- pruning is built; manual SQL when the table gets large.

create table listing_freshness_checks (
  id            bigserial primary key,
  sreality_id   bigint not null,
  checked_at    timestamptz not null default now(),
  outcome       text not null check (outcome in ('unchanged', 'updated', 'gone', 'fetch_error')),
  prev_hash     text,
  new_hash      text,
  error_message text
);

create index on listing_freshness_checks (sreality_id, checked_at desc);
create index on listing_freshness_checks (checked_at desc);

alter table listing_freshness_checks enable row level security;
