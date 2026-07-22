-- 352_estimation_job_lane.sql
-- Wave 1 (W1-3) — move agent/deterministic estimation EXECUTION off the FastAPI
-- request threadpool onto a job lane on the always-on realtime worker
-- (docs/design/waves-1-4-public-features.md § Wave 1, Phase 1 Amendment A10).
--
-- The run row stays the job — no new job table. It gains three columns:
--   * job_payload — the {body, resolution} execution snapshot serialized at
--     submit time, so the worker executes without re-parsing the URL (the parse
--     already ran in-request and its result is persisted on the row). NULL for
--     legacy/inline runs; the worker only ever claims WHERE job_payload IS NOT
--     NULL, so the in-request BackgroundTask executor and the lane never collide
--     during the flag flip. Cleared back to NULL once the run reaches a terminal
--     status (bounds storage — it can carry a full listing spec).
--   * claimed_at — when the worker lane picked the row up. The periodic stuck-run
--     sweep keys `running` rows off coalesce(claimed_at, created_at) so a
--     legitimately long agent run is timed from when it STARTED, not from when it
--     was queued. NULL for legacy background-task runs (keyed off created_at,
--     identical to today).
--   * worker — which worker instance holds the row (observability; today there is
--     exactly one).
--
-- All three are additive + nullable — no backfill, no behavior change until the
-- operator flips app_settings.estimation_job_lane_enabled. The lane ships dark.

begin;

alter table estimation_runs
  add column if not exists job_payload jsonb,
  add column if not exists claimed_at  timestamptz,
  add column if not exists worker      text;

-- The lane's claim: WHERE status='pending' AND job_payload IS NOT NULL
-- ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED. A tiny partial index keeps
-- that O(1) on the hot path once estimations go public + paid.
create index if not exists estimation_runs_pending_claim
  on estimation_runs (created_at)
  where status = 'pending';

commit;
