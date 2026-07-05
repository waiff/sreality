-- 271_dedup_runs_observability.sql
-- Run-row observability, part 4 (after 255 depth/claimed, 258 cleared/truncated-of-dirty,
-- 262 run_kind/truncated, 265 nullable gauges): persist the silent failure modes the
-- 2026-07 pipeline verification found. All were computed in-memory (or not at all) and
-- visible only in Actions logs, so a stalled pipeline looked idle instead of sick:
--
--   * skipped_unresolved   — pairs that reached the visual stage but could neither merge,
--                            dismiss, nor queue (free mode without a verdict). The candidate
--                            treadmill re-chewed ~296 such pairs every 2h, uncounted.
--   * oversized_groups /   — street groups / geo cells above MAX_GROUP_SIZE were skipped
--     skipped_oversized      WHOLE with only a LOG line: 342 groups holding 23,147 eligible
--                            listings (18.7% of the market) never compared. The engine now
--                            processes them BOUNDED (prioritized best pairs, capped); these
--                            columns count the groups hit and the pairs left on the table.
--   * vision_errors        — paid vision/LLM calls that raised (per run). The 2026-07 credit
--                            outage produced 38k+ failed calls over two days while every run
--                            row looked normal (auto_visual=0 was the only, ambiguous, tell).
--                            The engine also opens a breaker after 10 errors and degrades to
--                            warm-cache reads, so a dead key can't burn the wall-clock budget.
--   * truncated_cause      — 'deadline' | 'pair_cap': WHY a truncated run stopped.
--   * scan_groups_total /  — full-scan cursor telemetry: how many groups were ahead of the
--     scan_groups_scanned    cursor at run start and how many this run advanced past. Makes
--                            cycle pace (days-per-cycle) computable from run rows.
--   * dirty_age_p95_seconds— age of the dirty queue's claim at run start (p95); a rising
--     / dirty_pruned         value with pruned=0 is the starvation signal. Stamped by the
--                            dirty lane (a follow-up PR wires the values; columns land here
--                            so the public view is stable).
--   * runner               — 'actions' (scheduled/dispatch) vs 'worker' (the realtime
--                            worker's dedup lane, follow-up PR). NULL on historical rows.

alter table dedup_engine_runs
  add column if not exists skipped_unresolved    integer,
  add column if not exists skipped_oversized     integer,
  add column if not exists oversized_groups      integer,
  add column if not exists vision_errors         integer,
  add column if not exists truncated_cause       text,
  add column if not exists scan_groups_total     integer,
  add column if not exists scan_groups_scanned   integer,
  add column if not exists dirty_age_p95_seconds integer,
  add column if not exists dirty_pruned          integer,
  add column if not exists runner                text;

create or replace view dedup_engine_runs_public as
  select id, started_at, ended_at, eligible, flagged_location, flagged_disposition,
         pairs_considered, rejected, auto_address, auto_phash, auto_visual,
         queued, vision_calls, cost_usd, auto_dismissed, floor_plan_deferred, clip_deferred,
         dirty_queue_depth, dirty_claimed, dirty_cleared, dirty_truncated,
         run_kind, truncated,
         skipped_unresolved, skipped_oversized, oversized_groups, vision_errors,
         truncated_cause, scan_groups_total, scan_groups_scanned,
         dirty_age_p95_seconds, dirty_pruned, runner
  from dedup_engine_runs;

grant select on dedup_engine_runs_public to anon;

-- Cycle visibility for the /dedup dashboard: a single-row-per-lane view over the scan
-- frontier (cursor presence, cycle start, last completed cycle). The base table stays
-- RLS-denied to anon (machine state); this view exposes only the progress fields.
create or replace view dedup_scan_state_public as
  select lane,
         (cursor_key is not null) as mid_cycle,
         cycle_started_at,
         last_cycle_started_at,
         last_cycle_completed_at,
         updated_at
  from dedup_scan_state;

grant select on dedup_scan_state_public to anon;
