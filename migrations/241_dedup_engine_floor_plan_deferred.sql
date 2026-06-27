-- 241_dedup_engine_floor_plan_deferred.sql
-- The floor-plan validation gate (migration 234) gained a DEFER outcome: when the
-- engine WOULD merge a both-floor-plan pair but the Sonnet floor-plan verdict isn't
-- warmed yet (the daily --free run, before dedup_batches.yml has pre-warmed it), the
-- pair is neither merged unchecked nor dumped on the manual queue — it is DEFERRED
-- and re-tried next run once the batch lane warms the cache. Record how many pairs
-- deferred per run so a persistently-high count surfaces a broken floor-plan batch
-- lane (the otherwise-silent stall: deferred pairs would never merge). It should
-- trend to ~0 in steady state.
--
-- Additive: a new counter column (default 0 for historical rows) + appended to the
-- public view (anon reads the /dedup dashboard's per-run stat grid).

alter table dedup_engine_runs
  add column if not exists floor_plan_deferred integer not null default 0;

create or replace view dedup_engine_runs_public as
  select id, started_at, ended_at, eligible, flagged_location, flagged_disposition,
         pairs_considered, rejected, auto_address, auto_phash, auto_visual,
         queued, vision_calls, cost_usd, auto_dismissed, floor_plan_deferred
  from dedup_engine_runs;

-- CREATE OR REPLACE preserves the existing grant, but state it explicitly for
-- auditability (the convention for *_public views the anon dashboard reads).
grant select on dedup_engine_runs_public to anon;
