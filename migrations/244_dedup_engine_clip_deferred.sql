-- 244_dedup_engine_clip_deferred.sql
-- Wave 6 (CLIP-only mode, dedup_clip_only): when the room classifier is CLIP-only and a
-- rule-C pair has a CLIP-untagged listing, the engine re-queues that listing for the CLIP
-- tagger and DEFERS the pair (re-try once tagged) instead of paying Haiku. Record how many
-- pairs deferred per run — the same observability floor_plan_deferred got (migration 241):
-- a persistently-high count surfaces lagging CLIP coverage / a stalled tagger. 0 until the
-- operator flips dedup_clip_only on.
--
-- Additive: a new counter column (default 0 for historical rows) + appended to the public
-- view (anon reads the /dedup dashboard's per-run stat grid).

alter table dedup_engine_runs
  add column if not exists clip_deferred integer not null default 0;

create or replace view dedup_engine_runs_public as
  select id, started_at, ended_at, eligible, flagged_location, flagged_disposition,
         pairs_considered, rejected, auto_address, auto_phash, auto_visual,
         queued, vision_calls, cost_usd, auto_dismissed, floor_plan_deferred, clip_deferred
  from dedup_engine_runs;

grant select on dedup_engine_runs_public to anon;
