-- 551_retire_frozen_check_results.sql
-- Seven health checks with no producer left stop being served as "current".
--
-- ===========================================================================
-- DESTRUCTIVE (data, not schema). Operator OK 2026-09-22 ("proceed ... if there
-- are no consumers"). Consumers verified live before writing this file: the
-- only readers of pipeline_check_results are pipeline_checks_public (latest row
-- per check_key), pipeline_check_history_public (last 30 days) and
-- emit_verification_stale_alert (max(run_at) only); no view, function, trigger
-- or code path names any of the seven keys (repo grep: only tests over fake
-- rows and migration prose). Nothing depends on these rows existing.
--
-- WHY: pipeline_checks_public serves the NEWEST row per key with no recency
-- filter, so a check whose producer is gone stays on the Health page forever
-- at its last status. llm_liveness (retired by field-capture W0, #1561 -- no
-- recurring LLM producer is left to be silent about) froze at `fail` on
-- 2026-09-21; the six dedup-engine checks (rule 15 cutoff) froze on
-- 2026-08-06, three of them at `warn`. That is 1,822 rows presenting stale
-- verdicts as live ones. pipeline_check_results is observability, not history
-- (the history table is listing_snapshots); the open `sys:llm_liveness` bell
-- incident is NOT touched (notification_dispatches is append-only, rule 16) --
-- the operator marks that thread seen by hand.
--
-- BACKUP: statement 1 copies the rows into a plain table in the same database
-- before statement 2 deletes them -- a 1,822-row observability set does not
-- warrant an R2 pg_dump. Row counts at authoring (2026-09-22 03:40Z):
--   llm_liveness 1,162 (latest fail), engine_health 128 (warn), merge_latency
--   128 (warn), street_debt 128 (warn), geo_debt 128 (ok), eligibility_funnel
--   128 (ok), merge_precision_sample 20 (ok). Verify with:
--
--   select check_key, count(*) from pipeline_check_results
--    where check_key in ('llm_liveness','geo_debt','merge_latency',
--                        'eligibility_funnel','engine_health','street_debt',
--                        'merge_precision_sample') group by 1;
--
-- Apply via apply_migration.yml (statement autocommit; idempotent: the backup
-- table is created once and the delete finds nothing on a second run). No
-- browser role can read the backup: RLS on, no policies, no grants.
-- ===========================================================================

set lock_timeout = '5s';

create table if not exists pipeline_check_results_retired_2026_09_22
  (like pipeline_check_results including defaults);

insert into pipeline_check_results_retired_2026_09_22
select * from pipeline_check_results
 where check_key in ('llm_liveness', 'geo_debt', 'merge_latency', 'eligibility_funnel',
                     'engine_health', 'street_debt', 'merge_precision_sample')
   and not exists (select 1 from pipeline_check_results_retired_2026_09_22 b
                    where b.id = pipeline_check_results.id);

alter table pipeline_check_results_retired_2026_09_22 enable row level security;
revoke all on pipeline_check_results_retired_2026_09_22 from anon, authenticated, public;

delete from pipeline_check_results
 where check_key in ('llm_liveness', 'geo_debt', 'merge_latency', 'eligibility_funnel',
                     'engine_health', 'street_debt', 'merge_precision_sample');
