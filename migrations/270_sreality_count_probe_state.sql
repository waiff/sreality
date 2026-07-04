-- 270_sreality_count_probe_state.sql
--
-- Per-(category_main_cb, category_type_cb) result-total ledger for the realtime
-- worker's sreality count-probe lane (realtime-scrapers W3). sreality's v1
-- search API IGNORES every sort param, so the newest-first delta probe the
-- HTML/GraphQL portals use is impossible for it — the only cheap "did something
-- appear/disappear" signal is pagination.total per category. The lane fetches
-- that one number (limit=0, one request) for each of the ~20 (cm, ct) pairs
-- every realtime_sreality_count_interval_seconds and, on a change beyond the
-- +-1 jitter band, can trigger a targeted index_walk sooner than the next */15
-- cron tick. Latest-wins, one row per pair; internal worker state.

create table sreality_count_probe_state (
  category_main_cb integer     not null,
  category_type_cb integer     not null,
  last_total       integer,
  last_checked_at  timestamptz not null default now(),
  last_changed_at  timestamptz,
  primary key (category_main_cb, category_type_cb)
);

alter table sreality_count_probe_state enable row level security;
-- No anon policy + explicit revoke (the migration-237 lesson: Supabase's
-- schema-wide default grant would leave the table anon-writable had RLS ever
-- been flipped off): internal worker state, service-role only.
revoke all on sreality_count_probe_state from anon, authenticated;
