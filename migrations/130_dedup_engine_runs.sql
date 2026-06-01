-- 130_dedup_engine_runs.sql
-- Dedup engine rebuild: per-run observability for the autonomous engine, so the
-- /dedup automation dashboard can show what it did each run (how many listings
-- were eligible, how many auto-merged by which path, how many queued for the
-- operator). One row per `scripts.dedup_engine` invocation.
--
-- Mirrors scrape_runs (migration 086): a flat counters table, started/ended
-- timestamps, no FKs. The dashboard reads it through an anon public view
-- (same posture as scrape_runs_public, migration 100) — the counts are
-- non-sensitive operational stats.

create table dedup_engine_runs (
  id                     bigserial primary key,
  started_at             timestamptz not null default now(),
  ended_at               timestamptz,
  -- rule A eligibility snapshot at run start
  eligible               int not null default 0,
  flagged_location       int not null default 0,
  flagged_disposition    int not null default 0,
  -- candidate funnel
  pairs_considered       int not null default 0,
  rejected               int not null default 0,
  -- autonomous actions, by path
  auto_address           int not null default 0,  -- rule B: exact street+no.+disp+floor
  auto_phash             int not null default 0,  -- rule D layer 1: >=2 identical interior photos
  auto_visual            int not null default 0,  -- rule D layer 3: a High forensic verdict
  queued                 int not null default 0,  -- rule E: left for the operator
  -- cost
  vision_calls           int not null default 0,
  cost_usd               numeric(10, 6) not null default 0,
  notes                  text
);

create index on dedup_engine_runs (started_at desc);

alter table dedup_engine_runs enable row level security;

-- Anon-readable view for the dashboard (no secrets; same pattern as
-- scrape_runs_public). The dashboard also reads property_merge_events through
-- the existing /dedup API for the reversible recent-actions feed.
create view dedup_engine_runs_public as
select
  id, started_at, ended_at,
  eligible, flagged_location, flagged_disposition,
  pairs_considered, rejected,
  auto_address, auto_phash, auto_visual, queued,
  vision_calls, cost_usd
from dedup_engine_runs;

grant select on dedup_engine_runs_public to anon;
