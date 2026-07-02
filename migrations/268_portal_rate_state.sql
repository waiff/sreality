-- 268_portal_rate_state.sql
--
-- Shared per-portal politeness ledger (realtime-scrapers Wave C-1). The
-- in-process RateLimiter is per-runtime: a Railway worker beside the GitHub
-- Actions walks would double-hit portals with two independent budgets, and a
-- 429/403 penalty learned in one process never reached the other. This table
-- is the ONE budget both runtimes draw on: `scraper/rate_ledger.py` leases
-- batches of request slots by atomically advancing next_slot_at (one short
-- autocommit UPDATE per lease) and paces locally between leased slots.
--
-- - next_slot_at:   the shared frontier — the earliest instant the next
--                   request slot may start, across ALL runtimes.
-- - interval_ms:    the configured per-request spacing (last lease's config
--                   wins, so an operational_limits edit propagates).
-- - penalty_factor: shared adaptive multiplier — a 429/403 in any runtime
--                   widens it (capped), healthy leases decay it back to 1.0.
-- - penalized_at:   when the last penalty landed (observability).
--
-- Rows are created lazily per source on first use (INSERT ... ON CONFLICT DO
-- NOTHING from the ledger); nothing is seeded here.

create table portal_rate_state (
  source          text primary key,
  next_slot_at    timestamptz not null default now(),
  interval_ms     int not null,
  penalty_factor  real not null default 1.0,
  penalized_at    timestamptz,
  updated_at      timestamptz not null default now()
);

alter table portal_rate_state enable row level security;
-- No anon policy: internal scraper coordination state, service-role only.
