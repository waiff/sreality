-- 387_location_w1_claim_batch_resume_cursor.sql
--
-- Location-data W1: make a budget-stopped claims-intake run RESUMABLE, and stop
-- it from advancing the incremental watermark.
--
-- THE BUG THIS CLOSES. `location_claim_batches` (migration 382) records only a
-- terminal `outcome`, and the intake lane stamped every run that did not raise
-- as 'ok' — including a run that stopped because it hit `--max-seconds` or
-- `--limit`. The incremental watermark is `max(started_at) WHERE outcome='ok'`,
-- so a run that scanned the first 30k listings of 650k and stopped moved the
-- watermark past the 620k it never opened. Those rows are only re-visited if
-- `last_seen_at` moves again — which for the ~270k delisted rows it never will,
-- and a delisted row's payload is precisely the evidence the history waves need.
-- The mirror-image failure in `--mode full`: with no cursor to resume from,
-- every budgeted full pass restarts at id 0 and re-scans the same prefix forever.
--
-- THE FIX, in three additive columns and one widened CHECK:
--   * `scan_mode` — a full-mode cursor is a bare `listings.id` keyset and an
--     incremental one is `(last_seen_at, id)`. Resuming one from the other would
--     skip an arbitrary slice, so the cursor carries the mode that produced it
--     and is only ever read back by the same mode.
--   * `cursor_after_id` / `cursor_after_ts` — the keyset position the run
--     actually reached. NULL until the run's first batch commits.
--   * `resumable` — false when the run was started at an operator-chosen
--     `--start-after-id`. Its cursor does NOT mean "everything below is
--     scanned", so the resume lookup must not see it at all (the same guard
--     migration 385 puts on `mapy_inventory_runs`, and for the same reason).
--   * outcome gains 'stopped': a terminal state that is NOT a success. It is
--     invisible to the watermark query (which reads 'ok' only), so a budgeted
--     run leaves the incremental floor exactly where it found it.
--   * `coverage_since` — when the scan this run FINISHES began, which for a
--     resumed chain is not when this run began. The incremental watermark is
--     "everything written before this instant has been mined", and a chain of
--     three budgeted runs only proves that back to the FIRST one's start; taking
--     the last run's `started_at` would skip anything re-scraped underneath the
--     chain while it was walking. A fresh scan defaults it to its own start, so
--     the unresumed case is unchanged.
--
-- 'ok' now means one thing and one thing only: the scan ran out of rows. That is
-- the only state from which the watermark may move.
--
-- Purely additive: no column is dropped, no existing row is rewritten (the
-- outcome CHECK is widened, never narrowed), nothing outside
-- `location_claim_batches` is touched.

-- `set local`, not `set`: this file is applied inside a transaction, and a
-- session-scoped SET would leak the timeout onto whatever the pooled backend
-- serves next.
set local lock_timeout = '5s';

alter table location_claim_batches
  add column scan_mode       text check (scan_mode in ('full', 'incremental')),
  add column cursor_after_id bigint,
  add column cursor_after_ts timestamptz,
  add column coverage_since  timestamptz not null default now(),
  add column resumable       boolean not null default true;

alter table location_claim_batches
  drop constraint location_claim_batches_outcome_check;

alter table location_claim_batches
  add constraint location_claim_batches_outcome_check
  check (outcome in ('running', 'ok', 'stopped', 'failed', 'retracted'));

-- The resume lookup: newest terminal batch for one (lane, source, scan_mode)
-- among the resumable ones. Tiny table, but the lane runs hourly and the query
-- is on the critical path of every run.
create index lcb_resume
  on location_claim_batches (lane, source, scan_mode, started_at desc)
  where resumable;

comment on column location_claim_batches.scan_mode is
  'Which keyset the cursor belongs to: full = listings.id, incremental = '
  '(last_seen_at, id). A cursor is only ever resumed by the mode that wrote it.';
comment on column location_claim_batches.coverage_since is
  'When the SCAN this batch finishes began — its own started_at for a fresh scan, '
  'the first run''s for a resumed chain. The incremental watermark reads this, not '
  'started_at: three budgeted runs only prove coverage back to the first one''s start.';
comment on column location_claim_batches.resumable is
  'False when the run was anchored at an operator-chosen --start-after-id: its '
  'cursor does not certify that everything below it was scanned, so the resume '
  'lookup ignores the row entirely.';
comment on column location_claim_batches.outcome is
  'Terminal state. ''ok'' means the scan REACHED THE END OF ITS RANGE and is the '
  'only state the incremental watermark reads; ''stopped'' means a --limit / '
  '--max-seconds budget ended it early, leaving the watermark where it was and '
  'the cursor to resume from.';
