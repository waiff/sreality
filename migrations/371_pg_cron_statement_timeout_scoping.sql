-- 371_pg_cron_statement_timeout_scoping.sql
--
-- Fixes a live defect (surfaced + live-verified 2026-08-05) affecting FOUR pg_cron
-- jobs. PostgreSQL arms statement_timeout exactly once, at the moment a top-level
-- statement begins, using whatever value is active in the session at that instant --
-- and per the Postgres documentation this is true even for a SET (LOCAL or
-- otherwise) issued *from inside* the function that top-level statement calls: a
-- function cannot raise its own execution budget by setting statement_timeout in
-- its body or proconfig, in ANY calling context. Three functions here were written
-- believing otherwise -- rebuild_browse_list() and rebuild_properties_map_mv()
-- (migration 277) declare `set statement_timeout = '600s'` in their proconfig, and
-- refresh_health_matviews() (migration 284) declares `'300s'` -- and all three are
-- scheduled via a bare `select fn();` pg_cron command with nothing upstream to
-- raise the limit. Every one of them actually runs capped at Postgres's 120s
-- database default, not the budget the author intended.
--
-- Live impact confirmed 2026-08-05 (trailing 48h): jobid 1 (health dashboard)
-- 29/288 runs failed (~10%), jobid 6 (browse list, */5) 75/576 failed (~13%,
-- spiking to 30-50% during busy windows), jobid 7 (browse map) 6/96 failed (~6%)
-- -- every failure at exactly 120.0s, every success comfortably inside the
-- intended budget (33-98s). Migration 284 already tried to fix this exact symptom
-- for the health dashboard on 2026-07-09 (PR #734) using the same ineffective
-- proconfig-only approach, so this is the second independent time the mistake
-- shipped -- evidence it's a systemic misunderstanding, not a typo, which is why a
-- permanent CI guard (tests/test_cron_statement_timeout_guard.py, same PR)
-- accompanies this fix.
--
-- A fourth job, dedup-funnel-mv-refresh (migration 282), has no override at all and
-- was observed at 120.5s -- zero margin without having failed yet. Hardened here
-- pre-emptively.
--
-- Fix, two parts:
--  1. Put the real budget where it actually works: a `set statement_timeout` issued
--     as its OWN statement ahead of the call, inside the SAME pg_cron command.
--     cron.schedule() upserts by job name (idempotent -- confirmed by migrations
--     136/176/277/284's own re-applies), so re-issuing it here is the
--     version-controlled equivalent of a live-only `cron.alter_job` fix: it
--     replays correctly in CI and any fresh environment.
--  2. Drop the now-documented-dead `set statement_timeout` from each function's own
--     proconfig -- it has never done anything for that function's own execution, in
--     any calling context, so leaving it in place only invites a third repeat of
--     this exact mistake by anyone who copies the pattern believing it works.
--     `search_path` is a normal GUC, unaffected by this caveat, and stays.
--
-- No behavior change beyond making the already-intended timeout real. Function
-- bodies are byte-identical; only each CREATE FUNCTION's option clauses and the
-- four job commands change.
--
-- ci-allow-dynamic: rebuild_browse_list / rebuild_properties_map_mv build their
-- blue-green replacement objects through EXECUTE, inherited verbatim (unchanged)
-- from migration 277 -- pre-existing, already-reviewed dynamic DDL, not new here.
--
-- ci-allow-ungated: refresh_health_matviews only issues REFRESH MATERIALIZED VIEW
-- CONCURRENTLY (returns void) -- a write-only maintenance action, not a read path
-- that exposes admin-only rows to any caller, so the is_platform_admin() read-gate
-- rule does not apply to it.

set local lock_timeout = '5s';

-------------------------------------------------------------------
-- 1. Functions: drop the dead proconfig statement_timeout.
-------------------------------------------------------------------
create or replace function rebuild_browse_list()
returns void
language plpgsql
security definer
set search_path = public
as $fn$
declare
  t0 timestamptz := clock_timestamp();
  n  bigint;
begin
  if not pg_try_advisory_lock(hashtext('rebuild_browse_list')) then
    raise notice 'rebuild_browse_list: previous run still active, skipping tick';
    return;
  end if;
  begin
    execute 'drop table if exists browse_list_next';
    execute $q$
      create unlogged table browse_list_next as
      select * from browse_projection
      order by category_main, category_type, first_seen_at
    $q$;
    execute 'create unique index browse_list_next_pk on browse_list_next (property_id)';
    execute 'create index browse_list_next_cat_first_seen_idx on browse_list_next (category_main, category_type, first_seen_at desc, property_id desc)';
    execute 'create index browse_list_next_obec_price_idx on browse_list_next (obec_id, category_type, price_czk) where obec_id is not null';
    execute 'create index browse_list_next_okres_price_idx on browse_list_next (okres_id, category_type, price_czk) where okres_id is not null';
    execute 'create index browse_list_next_region_price_idx on browse_list_next (region_id, category_type, price_czk) where region_id is not null';
    execute 'analyze browse_list_next';
    execute 'select count(*) from browse_list_next' into n;

    execute 'drop table if exists browse_list';
    execute 'alter table browse_list_next rename to browse_list';
    execute 'alter index browse_list_next_pk rename to browse_list_pk';
    execute 'alter index browse_list_next_cat_first_seen_idx rename to browse_list_cat_first_seen_idx';
    execute 'alter index browse_list_next_obec_price_idx rename to browse_list_obec_price_idx';
    execute 'alter index browse_list_next_okres_price_idx rename to browse_list_okres_price_idx';
    execute 'alter index browse_list_next_region_price_idx rename to browse_list_region_price_idx';
    execute 'grant select on browse_list to anon, authenticated';

    update browse_read_model_state
       set list_rebuilt_at  = now(),
           list_duration_ms = (extract(epoch from clock_timestamp() - t0) * 1000)::integer,
           list_rows        = n
     where id = 1;
    perform pg_notify('pgrst', 'reload schema');
  exception when others then
    perform pg_advisory_unlock(hashtext('rebuild_browse_list'));
    raise;
  end;
  perform pg_advisory_unlock(hashtext('rebuild_browse_list'));
end
$fn$;

create or replace function rebuild_properties_map_mv()
returns void
language plpgsql
security definer
set search_path = public
as $fn$
declare
  t0 timestamptz := clock_timestamp();
  n  bigint;
begin
  if not pg_try_advisory_lock(hashtext('rebuild_properties_map_mv')) then
    raise notice 'rebuild_properties_map_mv: previous run still active, skipping tick';
    return;
  end if;
  begin
    execute 'drop materialized view if exists properties_map_mv_next';
    execute $q$
      create materialized view properties_map_mv_next as
      select * from browse_projection
      where lat is not null and lng is not null
      order by category_main, category_type, lat, lng
    $q$;
    execute 'create unique index properties_map_mv_next_pk on properties_map_mv_next (property_id)';
    execute $q$
      create index properties_map_mv_next_cover on properties_map_mv_next
        (category_main, category_type, lat, lng)
        include (sreality_id, price_czk, disposition, subtype, area_m2, district,
                 last_seen_at, first_seen_at, is_active)
    $q$;
    execute 'analyze properties_map_mv_next';
    execute 'select count(*) from properties_map_mv_next' into n;

    execute 'drop materialized view if exists properties_map_mv';
    execute 'alter materialized view properties_map_mv_next rename to properties_map_mv';
    execute 'alter index properties_map_mv_next_pk rename to properties_map_mv_pk';
    execute 'alter index properties_map_mv_next_cover rename to properties_map_mv_cover';
    execute 'grant select on properties_map_mv to anon, authenticated';

    update browse_read_model_state
       set map_rebuilt_at  = now(),
           map_duration_ms = (extract(epoch from clock_timestamp() - t0) * 1000)::integer,
           map_rows        = n
     where id = 1;
    perform pg_notify('pgrst', 'reload schema');
  exception when others then
    perform pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));
    raise;
  end;
  perform pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));
end
$fn$;

create or replace function public.refresh_health_matviews()
returns void
language plpgsql
security definer
set search_path to 'public'
as $function$
begin
  refresh materialized view concurrently health_summary_mv;
  refresh materialized view concurrently portal_health_mv;
  refresh materialized view concurrently snapshot_churn_24h_mv;
  refresh materialized view concurrently scraper_health_checks_mv;
  refresh materialized view concurrently category_trends_mv;
  refresh materialized view concurrently health_mv_refresh_stamp;
end;
$function$;

-------------------------------------------------------------------
-- 2. Schedules: re-arm statement_timeout in the cron command itself, the only
--    place it actually takes effect. cron.schedule() upserts by job name, so
--    this updates the 4 existing jobs in place (guarded: CI's pg_cron-less
--    replay logs a notice and skips, same as every prior pg_cron migration).
-------------------------------------------------------------------
do $cron$
begin
  create extension if not exists pg_cron;
  perform cron.schedule(
    'refresh-health-dashboard',
    '*/10 * * * *',
    $$set statement_timeout='300s'; select public.refresh_health_matviews();$$
  );
  perform cron.schedule(
    'browse-list-rebuild',
    '*/5 * * * *',
    $$set statement_timeout='600s'; select public.rebuild_browse_list();$$
  );
  perform cron.schedule(
    'browse-map-rebuild',
    '7,37 * * * *',
    $$set statement_timeout='600s'; select public.rebuild_properties_map_mv();$$
  );
  perform cron.schedule(
    'dedup-funnel-mv-refresh',
    '*/15 * * * *',
    $$set statement_timeout='300s';
      refresh materialized view concurrently dedup_funnel_resolutions_mv;
      refresh materialized view concurrently dedup_llm_cost_by_category_mv;$$
  );
exception when others then
  raise notice 'pg_cron unavailable; statement_timeout scoping fix not applied (%). Jobs unaffected in this environment.', sqlerrm;
end
$cron$;
