-- Single-row mutual-exclusion lock for broker resolution.
--
-- The full sweep (resolve_brokers_full.yml) and the */10 incremental
-- (broker_resolution.yml) must never mutate brokers concurrently. GitHub
-- concurrency can't be the mechanism: a shared group with cancel-in-progress:false
-- CANCELS a *pending* run when a newer same-group run queues, so the */10 incremental
-- starves the daily full sweep. And a session-level pg_advisory_lock is unreliable
-- through the transaction-mode pooler (no pinned backend). This table is a
-- pooler-safe claim instead: acquire is one atomic UPDATE, the holder heartbeats
-- during a long run, and a stale heartbeat lets a later run take over after a
-- SIGKILL. The incremental yields (skips) when the lock is held; the full sweep
-- waits/takes-over because it is the reconcile that must run.

CREATE TABLE IF NOT EXISTS broker_resolution_lock (
  id           int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  holder       text,
  mode         text,
  acquired_at  timestamptz,
  heartbeat_at timestamptz
);

INSERT INTO broker_resolution_lock (id, heartbeat_at)
VALUES (1, now() - interval '1 day')
ON CONFLICT (id) DO NOTHING;
