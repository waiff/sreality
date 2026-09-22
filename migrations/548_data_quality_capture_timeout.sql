-- 548_data_quality_capture_timeout.sql — repair the data-quality capture (field capture W1).
--
-- DECIDED ON EVIDENCE: REPAIR, NOT RETIRE. The W1 brief called this capture dead and
-- proposed unscheduling it. Three facts read live on 2026-09-21 say otherwise.
--
-- 1. IT IS NOT DEAD, IT IS FLAKY. `cron.job_run_details` for jobid 3 over the last ten
--    days: 31 failed, 9 succeeded, and the most recent run (18:30Z) SUCCEEDED with
--    `INSERT 0 234` in 172.6 s. Every failure is `canceling statement due to statement
--    timeout` or `job startup timeout` — the job's command carries no `set
--    statement_timeout` prefix, unlike jobid 6 (browse rebuild, 1800s), jobid 7 (map
--    rebuild, 1500s), jobid 17 (pin audit, 900s) and jobid 1 (health matviews, 300s).
--    One missing line, not a dead instrument.
--
-- 2. IT IS READ, AND BY AN OPERATOR-FACING PAGE. The one relation reading
--    `data_quality_snapshots` (pg_depend) is the `scraper_health_checks_mv` matview and
--    its `field_null_drift` rung (migration 354 § drift_fresh / drift_baseline), served
--    by the `scraper_health_checks` RPC that `frontend/src/lib/queries.ts` calls and
--    `frontend/src/pages/Health.tsx` renders. (NOT `field_null_drift_stat`: migration 179
--    added that helper and 215 dropped it — no such function exists today, so do not go
--    looking for one and conclude the table is unread.) Both arms need a capture < 20 h
--    old AND one 20 h–8 d old; at a 22.5 % success rate the fresh arm is usually missing,
--    so the rung reads "no baseline yet" instead of a number. Unscheduling the producer
--    would have blanked it silently.
--
-- 3. IT MEASURES THINGS THE NEW MATRIX CANNOT. W1's `field_fill_matrix` check is derived
--    from `scraper.db.LISTING_COLUMNS`, so it covers the 26 attribute COLUMNS and nothing
--    else. The view's 26 fields include `geom`, `locality`, `street`, `property_grouped`,
--    `source_url` and the two condition levels — seven probes with no attribute column of
--    their own. The brief's other claim, that the capture's field list "names columns
--    dropped by migration 508", is false: the job has no field list, it copies
--    `data_quality_by_source`, whose body 508 rewrote and which still emits all 26 today.
--
-- WHICH INSTRUMENT OWNS WHAT, so the two can never be read against each other: the view
-- keeps those seven probes, `field_fill_matrix` owns the LISTING_COLUMNS attributes, and
-- the 19 fields they share are the view's to lose when a later wave prunes it. Both now
-- measure the SAME cohort — every active row — so a shared cell cannot report two
-- different fill rates.
--
-- SO: 900 s, the same number the pin-audit refresh carries — 5x the measured 172.6 s run
-- and well under the 1800 s the browse rebuild legitimately takes. Nothing else changes:
-- the table keeps its 87,745 rows (history is sacred, CLAUDE.md rule 3) and the schedule
-- stays `30 */6 * * *`.
--
-- `cron.schedule` on an existing jobname UPDATES it, so this is idempotent; the exception
-- guard is 136 / 274 / 510 / 514 / 515 / 518 / 524's — the CI schema-replay container has
-- no pg_cron and this file must still apply there.

do $cron$
begin
  create extension if not exists pg_cron;
  perform cron.schedule(
    'capture-data-quality',
    '30 */6 * * *',
    $$set statement_timeout='900s';
      insert into public.data_quality_snapshots (source, field, n_active, n_populated, pct_populated)
      select source, field, n_active, n_populated, pct_populated from public.data_quality_by_source;$$
  );
exception when others then
  raise notice 'pg_cron unavailable; data-quality capture not rescheduled (%).', sqlerrm;
end
$cron$;
