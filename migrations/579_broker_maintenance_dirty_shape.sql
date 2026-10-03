-- 579_broker_maintenance_dirty_shape.sql
-- Broker Unify W3: broker maintenance moves onto the rule-#20 shape, and the
-- registry + queue reflect it.
--
-- RUN ORDER — DEPLOY THE CODE FIRST, THEN APPLY THIS FILE (migration 508's
-- rule, same reason): the pre-W3 writer still names sreality_id in its INSERT,
-- so dropping the column first would break every detail-drain broker enqueue
-- within one tick. The W3 writer inserts (listing_id) only and runs fine while
-- the column still exists, so the window between merge and apply is safe.
--
-- Code half (same PR): scripts/resolve_brokers.py grows run_incremental_pass
-- (drain-until-empty + stale-matview republish, THE one incremental driver);
-- the realtime worker gains a broker_maintenance lane calling it every
-- ~realtime_maintenance_interval_seconds; broker_resolution.yml stays as the
-- throttled GH backstop of the SAME driver; the daily full sweep's 9-window
-- rollup loop is replaced by one recompute_brokers(None) statement and the
-- sweep no longer publishes the matview at all.
--
-- 1) The leaderboard matview's registry row stops describing the old world.
--    Publication is now hourly-if-stale from the drain path (the stamp itself
--    is the clock — no new setting), so the 30 h special-case budget from
--    migration 440 ("its daily sweep itself runs up to 2h01m") loses its
--    reason to exist. 8 hours: quiet while only the throttled GH backstop is
--    alive (measured gaps up to ~6 h), red only when BOTH the worker lane and
--    the backstop are dead. The concurrent refresh itself measured 85 s
--    (2026-09-30, stamped by the migration-578 chokepoint).
update derived_artifacts
   set producer = 'scripts/resolve_brokers.py + broker_resolution.yml',
       host = 'realtime-worker',
       cadence = 'hourly-if-stale (broker drain)',
       staleness_budget = interval '8 hours'
 where name = 'broker_region_type_stats';

-- 2) DESTRUCTIVE (operator pre-approved 2026-09-30, dump-first): the legacy
--    dirty_broker_listings.sreality_id column. The R2 identity cutover left it
--    behind; migration 339 swapped the PK to listing_id and every writer now
--    inserts listing_id only (scraper/db.py x2, the property drain's W3
--    mirror), so the column is pure dead weight on a hot ON CONFLICT path.
--    The table is an ephemeral work queue (rows live minutes); the claimed
--    backup is a row snapshot taken immediately before apply.
alter table dirty_broker_listings drop column if exists sreality_id;
