-- 198_dedup_engine_auto_dismissed.sql
-- The dedup engine becomes self-healing: each run it auto-resolves stale
-- proposed candidates (dismiss deterministic non-matches + a calibrated visual
-- "different" verdict, merge the now-mergeable) instead of letting them pile up
-- in the /dedup queue. Track how many it auto-dismissed per run, alongside the
-- existing auto_address / auto_phash / auto_visual / queued counters.
--
-- Additive: a new counter column (default 0 for historical rows) + appended to
-- the public view (anon reads the dashboard).

alter table dedup_engine_runs
  add column auto_dismissed integer not null default 0;

create or replace view dedup_engine_runs_public as
  select id, started_at, ended_at, eligible, flagged_location, flagged_disposition,
         pairs_considered, rejected, auto_address, auto_phash, auto_visual,
         queued, vision_calls, cost_usd, auto_dismissed
  from dedup_engine_runs;
