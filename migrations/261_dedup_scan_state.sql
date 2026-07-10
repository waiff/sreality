-- 261_dedup_scan_state.sql
-- Cursor state for the dedup street FULL SCAN. The 6h full scan iterated street groups in a
-- deterministic order and hit its wall-clock deadline at ~9% of the market's pair slots EVERY
-- run — a head-restart with no cursor, so the tail ~91% of street groups was structurally never
-- re-scanned (the same "front plateau" pathology the geo lane hit). Worse, the dirty queue's TTL
-- eviction justified itself with "the full scan has already covered them", which was therefore
-- FALSE — eviction was silent work loss.
--
-- This table gives each scan lane a persistent frontier: successive runs resume AFTER cursor_key
-- (sorted street-group keys), and when a run reaches the end of the list the CYCLE is complete —
-- last_cycle_started_at/completed_at are stamped and the cursor resets. The dirty-queue TTL prune
-- now additionally requires `marked_at < last_cycle_started_at` (a row enqueued before a
-- completed cycle began is guaranteed to have had its groups scanned during that cycle), so
-- eviction NEVER discards uncovered work; with no completed cycle yet it evicts nothing.
--
-- `lane` keys the row ('street' today); the geo lane's scan state can unify onto this table when
-- its progressive-coverage work lands, rather than growing a parallel per-lane table.
create table dedup_scan_state (
  lane                   text primary key,
  cursor_key             text,
  cycle_started_at       timestamptz,
  last_cycle_started_at  timestamptz,
  last_cycle_completed_at timestamptz,
  updated_at             timestamptz not null default now()
);

-- Internal machine state; never read by the browser. No anon policy => RLS denies anon by
-- default (same posture as dedup_dirty_properties / listing_detail_queue).
alter table dedup_scan_state enable row level security;

comment on table dedup_scan_state is
  'Per-lane dedup scan frontier: full scans resume after cursor_key so the whole market is '
  'covered across runs (a cycle); last_cycle_* gate the dirty-queue TTL eviction so it only '
  'drops rows a COMPLETED cycle has provably covered.';
