-- 262_dedup_engine_runs_kind_truncated.sql
-- Run-row observability, part 3 (after 255 depth/claimed, 258 cleared/truncated-of-dirty):
-- make the RUN MODE and the RUN-LEVEL truncation first-class columns.
--
-- WHY: the 2026-07 dedup-lane audit found the composed system's safety story — "TTL-evicted
-- dirty rows are re-decided by the 6h full scan" — silently false in production: every street
-- FULL SCAN was hitting its wall-clock budget at ~9% of the market's pair slots, restarting
-- from the same deterministic obec-ASC head each time, and NOTHING recorded that. The engine
-- has always computed stats["truncated"] in-memory, but the run row never persisted it, and
-- run modes (full scan / candidate drain / dirty drain) were indistinguishable in the table —
-- dirty_* NULLness was the only tell. Persisting both makes full-scan coverage failure (and
-- any lane's chronic truncation) a queryable fact the dashboards + future coverage fixes
-- (the full-scan cursor) can key on.
--
--   * run_kind  — 'full' | 'candidates' | 'dirty' (text, not enum — the portals/curated-cities
--                 convention; a future kind is a code change, not a migration). The geo pass
--                 deliberately writes NO run row today (it would displace the street headline
--                 the /dedup dashboard reads); when that changes it becomes 'geo' here.
--   * truncated — 1 if the run stopped on its wall-clock / pair-cap budget before finishing
--                 its scan, 0 if it completed. Unlike 258's dirty_truncated (dirty-lane-only,
--                 NULL elsewhere) this is stamped on EVERY run row — the full scan's chronic
--                 truncation is the one this exists to expose.
--
-- started_at needs no DDL: the column has always existed (default now()) but the INSERT
-- omitted it, so started_at == ended_at on every row and durations were unrecorded; the
-- engine now passes the real run-start timestamp explicitly.

alter table dedup_engine_runs
  add column if not exists run_kind text,
  add column if not exists truncated integer;

create or replace view dedup_engine_runs_public as
  select id, started_at, ended_at, eligible, flagged_location, flagged_disposition,
         pairs_considered, rejected, auto_address, auto_phash, auto_visual,
         queued, vision_calls, cost_usd, auto_dismissed, floor_plan_deferred, clip_deferred,
         dirty_queue_depth, dirty_claimed, dirty_cleared, dirty_truncated,
         run_kind, truncated
  from dedup_engine_runs;

-- CREATE OR REPLACE preserves the existing grant, but state it explicitly for auditability
-- (the convention for *_public views the anon dashboard reads).
grant select on dedup_engine_runs_public to anon;
