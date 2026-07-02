-- 269_worker_heartbeats.sql
--
-- Liveness ledger for always-on workers (realtime-scrapers Wave C-3). The
-- realtime worker (scraper/realtime_worker.py — a second Railway service from
-- the same Docker image) upserts its beat here every ~30s: one latest-wins row
-- per worker. Deliberately NOT app_settings — its history trigger would append
-- an app_settings_history row per beat. `details` carries per-lane last-pass
-- timestamps + counters (probe / drain today; future lanes — images, dedup
-- wake — report into the same jsonb), so lane-grain observability needs no
-- schema change.

create table worker_heartbeats (
  worker     text primary key,
  beat_at    timestamptz not null default now(),
  started_at timestamptz not null,
  details    jsonb
);

alter table worker_heartbeats enable row level security;
-- No anon policy + explicit revoke (the migration-237 lesson: Supabase's
-- schema-wide default grant would otherwise leave the table anon-writable had
-- RLS ever been flipped off): internal worker state, service-role only.
revoke all on worker_heartbeats from anon, authenticated;

-- The Health-page hook for later: a dead worker is a growing age_seconds.
create view worker_liveness as
select worker,
       beat_at,
       extract(epoch from (now() - beat_at))::int as age_seconds
  from worker_heartbeats;

revoke all on worker_liveness from anon, authenticated;
