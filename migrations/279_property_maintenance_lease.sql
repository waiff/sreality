-- 279_property_maintenance_lease.sql
--
-- Pooler-proof mutual exclusion for property maintenance (fixes a defect in
-- PR #716, caught within minutes of deploy by the worker heartbeat).
--
-- #716 serialized the three maintenance writers (worker lane, GH cron, daily
-- full sweep) with a SESSION-level pg advisory lock. That is UNSOUND over the
-- transaction-mode pooler every Python path connects through (SUPABASE_DB_URL,
-- port 6543): under autocommit each statement is its own transaction and can
-- land on a DIFFERENT server backend, so pg_try_advisory_lock ran on backend X
-- while pg_advisory_unlock ran on backend Y — the unlock silently returned
-- false and the lock stayed stranded on X (observed live: the "holder" pid was
-- a Supavisor backend mid-way through an UNRELATED dedup statement, while every
-- maintenance pass skipped). Session advisory locks are only sound on direct or
-- session-pooled connections — which is why migration 277's pg_cron rebuild
-- functions (single local session per call) keep theirs.
--
-- The pooler-proof primitive is a LEASE ROW claimed by a SINGLE-STATEMENT
-- compare-and-set UPDATE — atomic on whatever backend it lands on, no session
-- state. Expiry self-heals a crashed holder (an incremental pass normally runs
-- seconds; its 15-minute lease is a wide margin — see run_incremental_pass).
-- Internal object: RLS on, no grants (only service-role writers touch it).

create table if not exists property_maintenance_lease (
  id         smallint primary key default 1 check (id = 1),
  holder     text,
  expires_at timestamptz
);
insert into property_maintenance_lease (id) values (1) on conflict (id) do nothing;
alter table property_maintenance_lease enable row level security;

comment on table property_maintenance_lease is
  'Single-row lease serializing property-maintenance writers (worker lane, GH '
  'cron, daily full sweep) across pooled connections — migration 279. Claimed '
  'by one atomic UPDATE ... RETURNING (scripts/recompute_property_stats.py); '
  'expiry self-heals a crashed holder. Replaces the #716 advisory lock, which '
  'is unsound over the transaction pooler.';
