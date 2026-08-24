-- 413: browse-list-rebuild cadence */5 -> */15 (hydration sprint W-1b).
--
-- WHY. Measured over 24h on 2026-08-24, jobid 6 (`browse-list-rebuild`):
--   201 runs · min 87s · median ~200s · avg 270s · 40 runs killed at the
--   600s statement_timeout · exactly ONE run exited fast on the advisory lock.
-- On a 300s schedule that is ~83% duty cycle: the job is doing a full CTAS of
-- browse_list (577k rows, 258 MB heap + 28 MB indexes) essentially without
-- pause, and its own daily wall-clock share matched that (80-84% on 08-20..23,
-- against 3.4% on 08-18 — the same function, so the cost is contention, not
-- intrinsic work).
--
-- The instance is I/O-bound, not CPU-bound: every non-idle backend sampled
-- during the incident was waiting on IO/DataFileRead (1 GB shared_buffers
-- against a 136 GB database, with a continuous scrape write load and
-- autovacuum on `listings` in the mix). A rebuild that rewrites a 286 MB
-- working set every five minutes is therefore both a victim of that pressure
-- and one of its largest contributors — it evicts the very pages the read path
-- then has to fault back in. That coupling is what made W-1a's ScalarArrayOp
-- scan (15,877 buffers) cross `authenticated`'s 8s statement_timeout and
-- return HTTP 500 on Browse's default view: cold-band reads right after a
-- rebuild are exactly when the plan was slowest.
--
-- WHAT THIS IS AND IS NOT. This is the mitigation half of W-1b: it takes the
-- single largest repeated I/O consumer from ~83% to ~28% duty cycle, which is
-- reversible, needs no code change, and buys headroom for the root-cause work.
-- It is NOT the root cause. Still open, in order:
--   (a) why the same rebuild costs 10s on a quiet day and 250-600s on a busy
--       one — profile it against the concurrent write load rather than in
--       isolation;
--   (b) whether the rebuild should be incremental off `dirty_properties`
--       (rule #20's pattern) instead of a full CTAS at any cadence;
--   (c) instance sizing / shared_buffers, which is a cost decision for the
--       operator and deliberately parked until (a) and (b) are answered.
--
-- FRESHNESS COST. browse_list may now lag up to ~15 min instead of ~5. In
-- practice the change is much smaller than it reads: at 270s average the
-- published snapshot was already 4-5 min old on arrival, and only 194 of 288
-- scheduled ticks actually completed, so the effective cadence was ~9 min
-- already. Merges stay read-your-writes regardless — `sync_browse_list()`
-- patches the affected rows inline in the merge transaction (rule #15), so the
-- cadence governs scrape-driven drift only, never operator-visible writes.
--
-- ROLLBACK. Re-run `cron.schedule('browse-list-rebuild', '*/5 * * * *', …)`
-- with the same command text; the schedule is the only thing this migration
-- changes. cron.schedule() upserts by job name.

do $cron$
begin
  create extension if not exists pg_cron;
  -- Command text is copied verbatim from the live job (migration 371 §2), so
  -- this migration changes the schedule and nothing else.
  perform cron.schedule(
    'browse-list-rebuild',
    '*/15 * * * *',
    $$set statement_timeout='600s'; select public.rebuild_browse_list();$$
  );
exception when others then
  raise notice 'pg_cron unavailable; browse-list-rebuild cadence unchanged (%).', sqlerrm;
end
$cron$;
