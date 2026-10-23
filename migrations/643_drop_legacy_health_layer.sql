-- 643_drop_legacy_health_layer.sql -- the old health layer goes: its 10-minute refresh, 8 matviews, 12 views,
-- 11 functions, 4 tables, 5 columns, 8 registry rows and 3 settings rows (Health dashboard sprint, wave 9 = Health
-- green sprint, wave 6; docs/design/health-dashboard.md, "Wave 9: Drop the old health layer" and section 14). The
-- plan and the headers of migrations 620 and 621 call this file 624; it is 643 as applied, because 624 would replay
-- before 625-640, which production applied first. Its sixth column, portals.scrape_cadence_minutes, goes in 663: 649
-- (applied before this file, replayed after it) inserts the web portal's row naming that column, and 650 (#1939)
-- must replay after this file (it drops parsed_url_cache, which parsed_url_activity reads), so no one number fits.
--
-- ===========================================================================
-- DESTRUCTIVE, with the operator's OK (ruling Q5; the apply on or after 2026-10-23 13:00Z, section 14). Applied only
-- after
--   (1) 48 h in which no browser role read a dropped view or called a dropped function (pg_stat_statements by
--       userid, against the baseline of 2026-10-19 23:50Z, so not before 2026-10-21 23:50Z), and a second 24 h
--       zero-reads check (section 14, wave 6);
--   (2) the R2 backup has been read back (backup_before_drop.yml, label health-legacy-643: the four tables and
--       data_quality_snapshots whole; the definitions of the 8 matviews and 12 views; ops_incidents' five engine
--       columns and portals.scrape_cadence_minutes as key + column CSVs; derived_artifacts and app_settings whole).
-- The code that still writes the old layer survives this file, so it is applied from the PR's branch before the merge
-- (the database skill's order): images.yml's refresh-stats job (scripts/refresh_image_stats.py) catches the
-- undefined_table public.refresh_matview raises for a dropped matview and skips; the by_category keys walk_coverage
-- and active_db are JSON in scrape_runs; resolve_brokers' daily lap would re-create broker_resolution_last_complete,
-- so the PR merges before its next run (04:35 UTC, GitHub cron) or that one unread row returns. The PR deletes all
-- three writers.
-- Forward only (rule 1): a restore is that backup plus the function statements of migrations 274, 318, 332, 340
-- and 441, which equal the live bodies but for whitespace and comments. No revert file (618 and 627 ship none).
-- The live functions, 2026-10-20: md5(prosrc), latest file, EXECUTE grantees beside the owner.
--   refresh_health_matviews()                d8f76edbd6b60e57e009345ca3bbe2b9  441  service_role
--   health_summary()                         3f8d6327ac411554fa0ad456f58c5f16  332  authenticated, service_role
--   portal_health_summary()                  bb9052d1182ac8ca57412c3fc05a10c7  332  authenticated, service_role
--   scraper_health_checks(text)              bd6c4d843c8fc055760fad6c75a8abb0  332  authenticated, service_role
--   category_trends(text, integer, integer)  4c5a5bfbc5a60bb60f519bc7a000b87d  332  authenticated, service_role
--   image_storage_overview()                 93249dec67a571571eef5cd52426e2a0  332  authenticated, service_role
--   images_failure_overview()                3308587db2afae01f1238a45702e7a78  318  authenticated, service_role
--   recent_scrape_runs(integer)              5e1dda40f942c9b674f9317749799ef8  340  authenticated, service_role
--   workflow_failure_summary(integer)        5b1d09046757da6aa610107ec028bbbd  318  authenticated, service_role
--   recent_workflow_failures(integer)        361ab399931d5f658c5113446c99effe  318  authenticated, service_role
--   emit_verification_stale_alert()          2ebb917d5e93fa6e99c60eeddd2e2996  274  service_role
-- ===========================================================================
--
-- WHY. Since wave 8 the Health page reads pipeline_checks_public only, and nothing reads the old layer: live
-- pg_depend, view and matview definitions, every function body and pg_cron command (2026-10-20), and git grep of
-- api/, toolkit/, scraper/, scripts/, frontend/src and chrome-extension/src. Its refresh still ran every 10
-- minutes: 288 runs and 2.2 h of database time in the 48 h to 2026-10-20.
--
-- WHAT GOES (nothing outside this list depends on it or names it: live, 2026-10-20)
--   1. The pg_cron job refresh-health-dashboard (jobid 1, */10). capture-data-quality went in 633,
--      emit-verification-stale-alert in 620.
--   2. Matviews health_summary_mv, portal_health_mv, scraper_health_checks_mv, snapshot_churn_24h_mv,
--      category_trends_mv, health_mv_refresh_stamp, image_storage_overview_mv and images_failure_overview_mv; each
--      takes its own unique index with it.
--   3. Views derived_artifacts_public, worker_liveness, detail_latency_recent, listing_presence_checks_public,
--      scrape_runs_public, listing_detail_queue_public, listing_fetch_failures_public, parsed_url_activity,
--      portals_public, portal_listing_counts, data_quality_by_source and pipeline_check_history_public. No view
--      reads another; the four matviews that read six of them go first.
--   4. Functions refresh_health_matviews(), health_summary(), portal_health_summary(), scraper_health_checks(text),
--      category_trends(text, integer, integer), image_storage_overview(), images_failure_overview(),
--      recent_scrape_runs(integer), workflow_failure_summary(integer), recent_workflow_failures(integer) and
--      emit_verification_stale_alert(): one overload each, dropped by its exact signature.
--   5. Tables pipeline_check_results_retired_2026_09_22 (551; 1,822 rows), portal_coverage_gate (455; 153),
--      dual_write_watermark (326; 22) and ops_incident_runs (463; 611, unwritten since 2026-10-18).
--   6. Columns ops_incidents.workflow_paths, origins, sample_run_url, sample_excerpt and failure_count (the signature
--      engine's, 462). No index or constraint covers any of them. (portals.scrape_cadence_minutes, 114: in 663.)
--   7. Rows: the eight matviews' derived_artifacts rows (18 -> 10 live); the app_settings rows
--      pipeline_check_thresholds, broker_resolution_last_complete and property_sweep_last_complete.
--   8. 621's statement 3 again: an open incident outside the cell namespace closes as 'retired: signature engine'
--      (0 rows live; it closes a row the old code reopened before its deploy, as 621 promised).
--   9. Comments: ops_incidents' (still 462's signature-engine text) restated for cell incidents; the two that name
--      a dropped view restated without it, pipeline_check_results' (274) and measure_plausibility_by_source's
--      (633); new ones on ops_incidents_open_signature_uq and data_quality_snapshots.
-- WHAT STAYS. data_quality_snapshots (ruling Q5: frozen history; no writer since 633, no reader after this file).
-- ops_incidents_open_signature_uq: the cell ladder's ON CONFLICT arbiter (toolkit/system_alerts._OPEN_SQL). 621's
-- header lists it with the engine's objects, but without it every incident open raises (42P10), so $pre$ requires
-- it and $post$ asserts it. ops_incidents' resolved engine rows as history (their engine columns go; the backup's
-- CSV keeps them) and ops_incidents_open_recent_idx. cron.job_run_details. Three items other documents hand to wave
-- 9 are not dropped, and the scraper track owns them: portals.supports_complete_walk (still read by
-- api/routes/admin.py and the Scrapers page), delist_flip_refusals (still written by db.enqueue_presence_checks) and
-- the 150 ceskereality and mmreality portal_index_slices rows (unjudged).
--
-- WHAT RUNS, in this order, in ONE transaction (lock_timeout 5 s, statement_timeout 120 s):
--   0. $pre$ refuses before any DROP while anything outside this list names or depends on what goes: a view or
--      matview definition; a function body in any non-catalog schema (pg_depend does not track a plpgsql or sql
--      body); a pg_depend entry on a dropped relation, its row type, a dropped function or a dropped column; an
--      index, constraint or other object a column's removal would take with it; an overload the DROPs below do not
--      spell; another pg_cron job calling any of it. It refuses too while ops_incidents_open_signature_uq is
--      missing. It REPORTS, never refuses on, a refresh run that has not finished.
--   1. The unschedule, by job id from its name (a missing job selects no rows), guarded for the CI replay.
--   2. health_summary_mv: a refresh holds it from its first statement to its end, so a run in flight ends before
--      this file takes anything else, and a run that starts later waits on it holding nothing and fails once this
--      commits (a run of a job that no longer exists; no Health cell reads it).
--   3. The six pg_cron-produced derived_artifacts rows, in the transaction that unschedules their producer. After
--      2, in the refresh's own lock order (its matview, then its row): the reverse could deadlock with a stamp.
--   4. The seven other matviews (scraper_health_checks_mv in a statement of its own, before snapshot_churn_24h_mv,
--      the one it reads), then the two image rows, after their matviews for the same reason as 3: the
--      refresh_matview chokepoint stamps a row while it holds that row's matview.
--   5. The 12 views. 6. The 11 functions. 7. The tables, ops_incident_runs last (dropping its FK locks
--      ops_incidents). 8. The columns. 9. The three settings rows. 10. 621's statement 3. 11. The comments.
--   12. $post$: everything above is gone; the arbiter, data_quality_snapshots, ops_incidents' posture and Health's
--       one read stand. Then notify pgrst (delivered at commit).
-- The hot lock (ops_incidents: the worker's health lane every 60 s, health-liveness every 5 min and the API) comes
-- last, so it is held only from 7 to the commit.
-- No CASCADE: the guard proves nothing else depends, and a plain DROP refuses otherwise. No ci-allow-dynamic marker:
-- no EXECUTE builds anything.
--
-- APPLY from the PR's branch once the gate holds and the backup is read back, then 663 at once:
-- apply_migration.yml --ref <branch> -f file=643_drop_legacy_health_layer.sql, a dry run, then dry_run=false
-- confirm=APPLY. Not while a resolve_brokers_full run is in flight (it would re-create broker_resolution_last_complete
-- after $post$). With psql running in minute :x3-:x4 or :x8-:x9: the */10 refresh holds health_summary_mv from :x0
-- for its whole run (p50 46 s, p95 151 s, max 375 s in the week to 2026-10-20), health-liveness (*/5) reads
-- ops_incidents, and rebuild_browse_list (*/15) holds its locks from :00, :15, :30 and :45 for ~70 s; a wait past
-- 5 s rolls the file back and the workflow's lock-timeout retry re-runs it.
-- HEALTH: the jobs and capacity cells of cron/refresh-health-dashboard and the eight artifact/<matview> cells leave on
-- their own (scripts/checks/platform.py builds them from live cron.job and derived_artifacts). migration_drift reads
-- this file Unverifiable (it creates nothing): its receipt is $post$, and the one-row verification SELECT in the PR.
-- A re-run is a no-op: every guard accepts an absent object, every DROP is IF EXISTS, and the DELETEs and the UPDATE
-- match nothing twice.
-- ROLLBACK: forward only (rule 1); the banner names the restore.

set lock_timeout = '5s';
set statement_timeout = '120s';

begin;

-- ---------------------------------------------------------------------------
-- 0. Preconditions, before any DROP.
-- ---------------------------------------------------------------------------
do $pre$
declare
  c_rels constant text[] := array[
    'health_summary_mv', 'portal_health_mv', 'scraper_health_checks_mv', 'snapshot_churn_24h_mv',
    'category_trends_mv', 'health_mv_refresh_stamp', 'image_storage_overview_mv', 'images_failure_overview_mv',
    'derived_artifacts_public', 'worker_liveness', 'detail_latency_recent', 'listing_presence_checks_public',
    'scrape_runs_public', 'listing_detail_queue_public', 'listing_fetch_failures_public', 'parsed_url_activity',
    'portals_public', 'portal_listing_counts', 'data_quality_by_source', 'pipeline_check_history_public',
    'pipeline_check_results_retired_2026_09_22', 'portal_coverage_gate', 'dual_write_watermark', 'ops_incident_runs'];
  c_fns constant text[] := array[
    'refresh_health_matviews', 'health_summary', 'portal_health_summary', 'scraper_health_checks',
    'category_trends', 'image_storage_overview', 'images_failure_overview', 'recent_scrape_runs',
    'workflow_failure_summary', 'recent_workflow_failures', 'emit_verification_stale_alert'];
  c_engine_cols constant text[] := array['workflow_paths', 'origins', 'sample_run_url', 'sample_excerpt',
                                         'failure_count'];
  v_names text := '\m(' || array_to_string(c_rels || c_fns, '|') || ')\M';
  v_engine text := '\m(' || array_to_string(c_engine_cols, '|') || ')\M';
  v_sigs oid[] := array_remove(array[
    to_regprocedure('public.refresh_health_matviews()')::oid,
    to_regprocedure('public.health_summary()')::oid,
    to_regprocedure('public.portal_health_summary()')::oid,
    to_regprocedure('public.scraper_health_checks(text)')::oid,
    to_regprocedure('public.category_trends(text, integer, integer)')::oid,
    to_regprocedure('public.image_storage_overview()')::oid,
    to_regprocedure('public.images_failure_overview()')::oid,
    to_regprocedure('public.recent_scrape_runs(integer)')::oid,
    to_regprocedure('public.workflow_failure_summary(integer)')::oid,
    to_regprocedure('public.recent_workflow_failures(integer)')::oid,
    to_regprocedure('public.emit_verification_stale_alert()')::oid], null);
  v_rels oid[];
  v_fns oid[];
  v_cols int[];
  v_bad text;
begin
  select coalesce(array_agg(c.oid), '{}') into v_rels
    from pg_class c
   where c.relnamespace = 'public'::regnamespace and c.relkind in ('r', 'v', 'm') and c.relname = any (c_rels);
  select coalesce(array_agg(p.oid), '{}') into v_fns
    from pg_proc p where p.pronamespace = 'public'::regnamespace and p.proname = any (c_fns);
  select coalesce(array_agg(a.attnum::int), '{}') into v_cols
    from pg_attribute a
   where a.attrelid = 'public.ops_incidents'::regclass and a.attname = any (c_engine_cols) and not a.attisdropped;

  if to_regclass('public.ops_incidents_open_signature_uq') is null then
    raise exception '643 refused: ops_incidents_open_signature_uq, the cell ladder''s ON CONFLICT arbiter, is missing; this file keeps it and runs only beside it';
  end if;

  -- DROP FUNCTION IF EXISTS on a signature that does not exist is a silent notice: an overload this file does not
  -- spell would survive it.
  select string_agg(p.oid::regprocedure::text, ', ' order by p.oid::regprocedure::text) into v_bad
    from pg_proc p where p.oid = any (v_fns) and p.oid <> all (v_sigs);
  if v_bad is not null then
    raise exception '643 refused: % is not a signature this file drops', v_bad;
  end if;

  select string_agg(x, ', ' order by x) into v_bad from (
    -- a view or matview outside the list naming what goes
    select format('view %I.%I', v.schemaname, v.viewname) as x
      from pg_views v
     where v.schemaname not in ('pg_catalog', 'information_schema')
       and not (v.schemaname = 'public' and v.viewname = any (c_rels))
       and v.definition ~ v_names
    union all
    select format('materialized view %I.%I', m.schemaname, m.matviewname)
      from pg_matviews m
     where not (m.schemaname = 'public' and m.matviewname = any (c_rels))
       and m.definition ~ v_names
    union all
    -- a function body outside the list naming what goes, or a dropped column beside its table's name
    select format('function %s', p.oid::regprocedure)
      from pg_proc p
     where p.pronamespace not in ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
       and p.oid <> all (v_fns)
       and (p.prosrc ~ v_names
            or (p.prosrc ~ '\mops_incidents\M' and p.prosrc ~ v_engine))
    union all
    -- an object outside the list on a dropped relation, its row type, a dropped function or a dropped column. What
    -- goes with a dropped relation (its rule, constraint, default, trigger or index: an auto or internal entry on
    -- it) is not outside: dual_write_watermark's CHECK constraint names its own columns, for one.
    select pg_describe_object(d.classid, d.objid, d.objsubid)
      from pg_depend d
     where d.deptype = 'n'
       and ((d.refclassid = 'pg_class'::regclass and d.refobjid = any (v_rels))
         or (d.refclassid = 'pg_type'::regclass
             and d.refobjid in (select c.reltype from pg_class c where c.oid = any (v_rels)))
         or (d.refclassid = 'pg_proc'::regclass and d.refobjid = any (v_fns))
         or (d.refclassid = 'pg_class'::regclass and d.refobjid = 'public.ops_incidents'::regclass
             and d.refobjsubid = any (v_cols)))
       and not exists (select 1 from pg_depend o
                        where o.classid = d.classid and o.objid = d.objid and o.deptype in ('a', 'i')
                          and o.refclassid = 'pg_class'::regclass and o.refobjid = any (v_rels))
       and not (d.classid = 'pg_proc'::regclass and d.objid = any (v_fns))
    union all
    -- what a column's removal would take with it unasked: an index, a constraint, a statistics object, a sequence
    -- (its own default goes with it, as it should)
    select pg_describe_object(d.classid, d.objid, d.objsubid)
      from pg_depend d
     where d.deptype = 'a'
       and d.classid <> 'pg_attrdef'::regclass
       and d.refclassid = 'pg_class'::regclass
       and d.refobjid = 'public.ops_incidents'::regclass and d.refobjsubid = any (v_cols)
  ) deps;
  if v_bad is not null then
    raise exception '643 refused: % still name(s) or depend(s) on what this file drops', v_bad;
  end if;

  if to_regclass('cron.job') is null then
    raise notice '643: no pg_cron here (the CI replay); the job checks are skipped';
    return;
  end if;
  select string_agg(j.jobname, ', ' order by j.jobname) into v_bad
    from cron.job j
   where j.jobname <> 'refresh-health-dashboard' and j.command ~ v_names;
  if v_bad is not null then
    raise exception '643 refused: pg_cron job(s) % call what this file drops', v_bad;
  end if;
  -- Reported, never refused on: a run in flight holds health_summary_mv until it ends, so step 2 waits out the 5 s
  -- lock limit and the workflow's retry re-runs the file.
  select string_agg(format('run %s %s, start %s', r.runid, r.status, coalesce(r.start_time::text, 'NULL')),
                    '; ' order by r.runid)
    into v_bad
    from cron.job_run_details r
    join cron.job j on j.jobid = r.jobid
   where j.jobname = 'refresh-health-dashboard'
     and r.status in ('starting', 'connecting', 'sending', 'running');
  if v_bad is not null then
    raise notice '643: refresh-health-dashboard run(s) not finished: %', v_bad;
  end if;
end
$pre$;

-- ---------------------------------------------------------------------------
-- 1. The job first. Guarded: the CI replay has no pg_cron; by id from its name, so a missing job selects no rows
--    (the name form raises on one). pg_cron sees the change at commit.
-- ---------------------------------------------------------------------------
do $cron$
begin
  if to_regclass('cron.job') is null then
    raise notice '643: no pg_cron here; nothing to unschedule';
  else
    perform cron.unschedule(j.jobid) from cron.job j where j.jobname = 'refresh-health-dashboard';
  end if;
end
$cron$;

-- ---------------------------------------------------------------------------
-- 2. The refresh's first lock, held to its end: a run in flight finishes before anything below is taken.
-- ---------------------------------------------------------------------------
drop materialized view if exists public.health_summary_mv;

-- ---------------------------------------------------------------------------
-- 3. The six rows the pg_cron refresh stamped, in the transaction that unschedules it (and after its first
--    matview: the matview, then the row, is the refresh's own order).
-- ---------------------------------------------------------------------------
delete from public.derived_artifacts
 where name in ('health_summary_mv', 'portal_health_mv', 'snapshot_churn_24h_mv', 'scraper_health_checks_mv',
                'category_trends_mv', 'health_mv_refresh_stamp');

-- ---------------------------------------------------------------------------
-- 4. The other seven matviews, scraper_health_checks_mv before the matview it reads, then the image pair's rows.
-- ---------------------------------------------------------------------------
drop materialized view if exists public.portal_health_mv;
drop materialized view if exists public.scraper_health_checks_mv;
drop materialized view if exists public.snapshot_churn_24h_mv;
drop materialized view if exists public.category_trends_mv;
drop materialized view if exists public.health_mv_refresh_stamp;
drop materialized view if exists public.image_storage_overview_mv;
drop materialized view if exists public.images_failure_overview_mv;
delete from public.derived_artifacts where name in ('image_storage_overview_mv', 'images_failure_overview_mv');

-- ---------------------------------------------------------------------------
-- 5. The views.
-- ---------------------------------------------------------------------------
drop view if exists public.derived_artifacts_public;
drop view if exists public.worker_liveness;
drop view if exists public.detail_latency_recent;
drop view if exists public.listing_presence_checks_public;
drop view if exists public.scrape_runs_public;
drop view if exists public.listing_detail_queue_public;
drop view if exists public.listing_fetch_failures_public;
drop view if exists public.parsed_url_activity;
drop view if exists public.portals_public;
drop view if exists public.portal_listing_counts;
drop view if exists public.data_quality_by_source;
drop view if exists public.pipeline_check_history_public;

-- ---------------------------------------------------------------------------
-- 6. The functions, by their exact signatures; the job's target first.
-- ---------------------------------------------------------------------------
drop function if exists public.refresh_health_matviews();
drop function if exists public.health_summary();
drop function if exists public.portal_health_summary();
drop function if exists public.scraper_health_checks(text);
drop function if exists public.category_trends(text, integer, integer);
drop function if exists public.image_storage_overview();
drop function if exists public.images_failure_overview();
drop function if exists public.recent_scrape_runs(integer);
drop function if exists public.workflow_failure_summary(integer);
drop function if exists public.recent_workflow_failures(integer);
drop function if exists public.emit_verification_stale_alert();

-- ---------------------------------------------------------------------------
-- 7. The tables; ops_incident_runs last, next to ops_incidents' own change (its FK locks that table).
-- ---------------------------------------------------------------------------
drop table if exists public.pipeline_check_results_retired_2026_09_22;
drop table if exists public.portal_coverage_gate;
drop table if exists public.dual_write_watermark;
drop table if exists public.ops_incident_runs;

-- ---------------------------------------------------------------------------
-- 8. The columns (catalog-only drops).
-- ---------------------------------------------------------------------------
alter table public.ops_incidents
  drop column if exists workflow_paths,
  drop column if exists origins,
  drop column if exists sample_run_url,
  drop column if exists sample_excerpt,
  drop column if exists failure_count;

-- ---------------------------------------------------------------------------
-- 9. The settings rows: the old checks' thresholds (read only by emit_verification_stale_alert, dropped above) and
--    two completion stamps nothing reads (the property sweep's unwritten since wave 4, the broker sweep's since
--    this PR).
-- ---------------------------------------------------------------------------
delete from public.app_settings
 where key in ('pipeline_check_thresholds', 'broker_resolution_last_complete', 'property_sweep_last_complete');

-- ---------------------------------------------------------------------------
-- 10. 621's statement 3, verbatim (starts_with, not LIKE: 619's idiom). 0 rows on 2026-10-20.
-- ---------------------------------------------------------------------------
update public.ops_incidents
   set resolved_at = now(), resolve_reason = 'retired: signature engine'
 where resolved_at is null
   and not starts_with(signature, 'cell:');

-- ---------------------------------------------------------------------------
-- 11. The comments.
-- ---------------------------------------------------------------------------
comment on table public.ops_incidents is
  'Cell incidents (migrations 619, 621 and 643): one row per failing health cell, signature '
  'cell:<check_key>:<subject>, at most one open per signature (ops_incidents_open_signature_uq). '
  'toolkit/system_alerts.reconcile opens it, keeps it (last_seen_at), holds its recovery (ok_since) and resolves '
  'it (resolved_at, resolve_reason), and tells each onset and reminder by stamping it (alert_count, alerted_at); '
  'Health shows it, the operator acknowledges it there (acknowledged_at, POST /admin/health/acknowledge) and the '
  'GitHub canary e-mails an alarm left unacknowledged; no system alert reaches the bell (migration 651). Rows '
  'outside the cell namespace are the retired signature '
  'engine''s history (migration 462), all resolved. RLS on and closed to browser roles; the browser reads '
  'incidents through pipeline_checks_public.';

comment on index public.ops_incidents_open_signature_uq is
  'The cell ladder''s ON CONFLICT arbiter (toolkit/system_alerts._OPEN_SQL: ON CONFLICT (signature) WHERE '
  'resolved_at IS NULL): at most one open incident per signature. Migration 621''s header lists it with the '
  'signature engine''s objects; migration 643 keeps it, because without it every incident open fails (42P10).';

comment on table public.data_quality_snapshots is
  'Frozen history (ruling Q5): per-source field fill, captured every 6 h until migration 633 unscheduled '
  'capture-data-quality (last capture 2026-10-19). Its view data_quality_by_source and its one reader, '
  'scraper_health_checks_mv, went in migration 643; the data_quality gauge replaced them. Nothing writes or '
  'reads it.';

comment on table public.pipeline_check_results is
  'One row per check measurement: scripts/verify_pipeline.py run_due (the GitHub canary and the worker''s '
  'health lane, which prunes results older than 35 days once a day) and the pg_cron watcher health_liveness() '
  '(key liveness). Service-role writes; the browser reads pipeline_checks_public (admin-gated).';

-- 633's text without its sentence naming data_quality_by_source (dropped above).
comment on view public.measure_plausibility_by_source is
  'Per (source, category_main, category_type) plausibility of the per-m2 measure over '
  'ACTIVE listings: the medians, the share of measurable rows the basis floor NULLs, and '
  'the share of rows whose area_m2 diverges from usable_area by more than 10%. Read by '
  'scripts/checks/quality.py once per health pass (the pass memo, scripts/checks/clock.PASS): '
  'the parsing alarm''s measures of new ads and median shift, and the data_quality gauge''s '
  'basis floor, area divergence, estate sum and measure gap (migration 633). Divergence is '
  'measured ONLY over rows carrying both areas (n_area_pairs) and only above a 10% relative '
  'band; for category_main = ''pozemek'' area_m2 is the PLOT by design (Option A) so divergence '
  'there is expected and the data_quality gauge skips those cells. Also publishes the '
  'DENOMINATORS every one of those ratios is blind to: n_area_valued / n_ppm2_valued are the '
  'support of the two medians (gate a median on its own support, never on n_active), and '
  'measure_input_gap_share is the share of active rows the measure has no input for: the arm '
  'that makes a cell with nothing to measure say so instead of reading clean. W21 appended '
  'estate_sum_share: the share of rows carrying all three areas whose estate_area equals '
  'area_m2 + usable_area within 1%, i.e. a side column holding a DERIVED SUM instead of a '
  'measurement, the class of defect mmreality shipped by reading a page-computed totalArea as '
  'the parcel, which neither a null-check nor the headline-vs-usable arm can see. ~12 s cold '
  'sequential scan (3.5 s warm on 2026-10-19): read it once per health pass, never per row.';

-- ---------------------------------------------------------------------------
-- 12. Post-conditions: everything above is gone; what stays stands.
-- ---------------------------------------------------------------------------
do $post$
declare
  v_left text;
  v_incidents text := coalesce(obj_description('public.ops_incidents'::regclass, 'pg_class'), '');
  v_results text := coalesce(obj_description('public.pipeline_check_results'::regclass, 'pg_class'), '');
  v_plausibility text := coalesce(obj_description('public.measure_plausibility_by_source'::regclass, 'pg_class'), '');
begin
  select string_agg(n, ', ' order by n) into v_left
    from unnest(array[
      'health_summary_mv', 'portal_health_mv', 'scraper_health_checks_mv', 'snapshot_churn_24h_mv',
      'category_trends_mv', 'health_mv_refresh_stamp', 'image_storage_overview_mv', 'images_failure_overview_mv',
      'derived_artifacts_public', 'worker_liveness', 'detail_latency_recent', 'listing_presence_checks_public',
      'scrape_runs_public', 'listing_detail_queue_public', 'listing_fetch_failures_public', 'parsed_url_activity',
      'portals_public', 'portal_listing_counts', 'data_quality_by_source', 'pipeline_check_history_public',
      'pipeline_check_results_retired_2026_09_22', 'portal_coverage_gate', 'dual_write_watermark',
      'ops_incident_runs']) as n
   where to_regclass('public.' || n) is not null;
  if v_left is not null then
    raise exception '643 did not land: % still exist(s)', v_left;
  end if;
  -- By name, any overload: a DROP with a wrong signature is a silent no-op.
  select string_agg(p.oid::regprocedure::text, ', ' order by p.oid::regprocedure::text) into v_left
    from pg_proc p
   where p.pronamespace = 'public'::regnamespace
     and p.proname in ('refresh_health_matviews', 'health_summary', 'portal_health_summary', 'scraper_health_checks',
                       'category_trends', 'image_storage_overview', 'images_failure_overview', 'recent_scrape_runs',
                       'workflow_failure_summary', 'recent_workflow_failures', 'emit_verification_stale_alert');
  if v_left is not null then
    raise exception '643 did not land: % still exist(s)', v_left;
  end if;
  select string_agg(format('%s.%s', a.attrelid::regclass, a.attname), ', ' order by a.attname) into v_left
    from pg_attribute a
   where not a.attisdropped
     and a.attrelid = 'public.ops_incidents'::regclass
     and a.attname in ('workflow_paths', 'origins', 'sample_run_url', 'sample_excerpt', 'failure_count');
  if v_left is not null then
    raise exception '643 did not land: column(s) % still exist', v_left;
  end if;
  -- Names, never a count: the registry holds other rows at this point of a replay than live.
  if exists (select 1 from public.derived_artifacts
              where name in ('health_summary_mv', 'portal_health_mv', 'scraper_health_checks_mv',
                             'snapshot_churn_24h_mv', 'category_trends_mv', 'health_mv_refresh_stamp',
                             'image_storage_overview_mv', 'images_failure_overview_mv')) then
    raise exception '643 did not land: a dropped matview''s derived_artifacts row survived';
  end if;
  if exists (select 1 from public.app_settings
              where key in ('pipeline_check_thresholds', 'broker_resolution_last_complete',
                            'property_sweep_last_complete')) then
    raise exception '643 did not land: a retired app_settings row survived';
  end if;
  if exists (select 1 from public.ops_incidents
              where resolved_at is null and not starts_with(signature, 'cell:')) then
    raise exception '643 did not land: an open incident outside the cell namespace survived';
  end if;
  if not exists (select 1 from pg_index i
                  where i.indexrelid = to_regclass('public.ops_incidents_open_signature_uq')
                    and i.indrelid = 'public.ops_incidents'::regclass
                    and i.indisunique and i.indisvalid
                    and pg_get_indexdef(i.indexrelid) like '%(signature) WHERE (resolved_at IS NULL)') then
    raise exception '643 did not land: ops_incidents_open_signature_uq, the ladder''s ON CONFLICT arbiter, is not a valid unique (signature) where resolved_at is null';
  end if;
  if to_regclass('public.data_quality_snapshots') is null then
    raise exception '643 did not land: data_quality_snapshots is gone (ruling Q5 keeps it)';
  end if;
  if strpos(v_incidents, 'cell:<check_key>:<subject>') = 0 or strpos(v_incidents, 'failure signature') > 0 then
    raise exception '643 did not land: the ops_incidents comment does not describe cell incidents';
  end if;
  if strpos(v_results, 'pipeline_check_history_public') > 0 or strpos(v_results, 'pipeline_checks_public') = 0 then
    raise exception '643 did not land: the pipeline_check_results comment still names a dropped view';
  end if;
  if strpos(v_plausibility, 'data_quality_by_source') > 0
     or strpos(v_plausibility, 'scripts/checks/quality.py') = 0 then
    raise exception '643 did not land: the measure_plausibility_by_source comment still names a dropped view';
  end if;
  if not (select c.relrowsecurity from pg_class c where c.oid = 'public.ops_incidents'::regclass)
     or exists (select 1 from unnest(array['anon', 'authenticated']::name[]) as r(role)
                 where has_any_column_privilege(r.role, 'public.ops_incidents', 'SELECT, INSERT, UPDATE, REFERENCES')
                    or has_table_privilege(r.role, 'public.ops_incidents', 'DELETE, TRUNCATE, TRIGGER')) then
    raise exception '643 did not land: ops_incidents must stay RLS-on and closed to browser roles';
  end if;
  if to_regclass('cron.job') is null then
    raise notice '643: no pg_cron here; the unschedule is not asserted';
  elsif exists (select 1 from cron.job where jobname = 'refresh-health-dashboard') then
    raise exception '643 did not land: refresh-health-dashboard is still scheduled';
  end if;
  -- Health's one read still answers; it reads only ops_incidents' kept columns.
  perform count(*) from public.pipeline_checks_public;
end
$post$;

notify pgrst, 'reload schema';

commit;

reset statement_timeout;
reset lock_timeout;
