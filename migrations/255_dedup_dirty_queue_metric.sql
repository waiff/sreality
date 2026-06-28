-- Observability for the real-time --dirty drain: record how deep the dedup-ready queue
-- (dedup_dirty_properties) was at each dirty run, and how many it claimed. A backlog that
-- stops draining (a new-portal tagging flood that out-paces --max-dirty, or a stalled drain)
-- is otherwise invisible — the 165K backlog that motivated the FIFO bound ran ~2 days unseen.
-- Both columns are NULL on non-dirty runs (full scan / candidate / geo).
alter table dedup_engine_runs
  add column if not exists dirty_queue_depth integer,
  add column if not exists dirty_claimed integer;

create or replace view dedup_engine_runs_public as
  select id, started_at, ended_at, eligible, flagged_location, flagged_disposition,
         pairs_considered, rejected, auto_address, auto_phash, auto_visual,
         queued, vision_calls, cost_usd, auto_dismissed, floor_plan_deferred, clip_deferred,
         dirty_queue_depth, dirty_claimed
  from dedup_engine_runs;

-- CREATE OR REPLACE preserves the existing grant, but state it explicitly for
-- auditability (the convention for *_public views the anon dashboard reads).
grant select on dedup_engine_runs_public to anon;
