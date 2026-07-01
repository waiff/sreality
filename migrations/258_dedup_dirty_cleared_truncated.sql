-- 258_dedup_dirty_cleared_truncated.sql
-- Real-time --dirty drain observability, part 2. migration 255 added dirty_queue_depth (how deep
-- the dedup-ready backlog was at run start) + dirty_claimed (this run's slice); those show a
-- non-draining queue but NOT *why*. These two add whether the run actually ADVANCED the head:
--   * dirty_cleared   — how many claimed properties the run deleted from the queue (0 on a
--                       truncated run, which keeps its whole claim).
--   * dirty_truncated — 1 if the run hit the deadline / pair-cap before finishing its slice.
-- cleared==0 while queue_depth stays high across successive runs is the silent LIVELOCK the FIFO
-- stall lacked a signal for (the drain re-claimed the same head every hour and never cleared).
-- Both NULL on non-dirty runs (full scan / candidate / geo).
alter table dedup_engine_runs
  add column if not exists dirty_cleared integer,
  add column if not exists dirty_truncated integer;

create or replace view dedup_engine_runs_public as
  select id, started_at, ended_at, eligible, flagged_location, flagged_disposition,
         pairs_considered, rejected, auto_address, auto_phash, auto_visual,
         queued, vision_calls, cost_usd, auto_dismissed, floor_plan_deferred, clip_deferred,
         dirty_queue_depth, dirty_claimed, dirty_cleared, dirty_truncated
  from dedup_engine_runs;

-- CREATE OR REPLACE preserves the existing grant, but state it explicitly for auditability
-- (the convention for *_public views the anon dashboard reads).
grant select on dedup_engine_runs_public to anon;
