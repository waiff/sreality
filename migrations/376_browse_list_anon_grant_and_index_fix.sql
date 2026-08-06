-- 376_browse_list_anon_grant_and_index_fix.sql
--
-- Migration 371 (pg_cron_statement_timeout_scoping) redefined
-- rebuild_browse_list() and rebuild_properties_map_mv() to fix an unrelated
-- statement_timeout bug, but its own header claimed "Function bodies are
-- byte-identical; only each CREATE FUNCTION's option clauses and the four
-- job commands change" -- false for both functions. Live-verified
-- (2026-08-06, has_table_privilege / pg_get_functiondef against project
-- erlvtprrmrylhznfyaih):
--
--   * `execute 'grant select on browse_list to anon, authenticated'` --
--     reopens the anon SELECT that migration 299 (Phase 0 emergency
--     hardening) explicitly narrowed to authenticated-only. Same for
--     properties_map_mv (not previously reported, found by comparing the
--     two functions' bodies side by side -- 371 regressed BOTH, not just
--     browse_list).
--   * The `revoke insert, update, delete, truncate on browse_list from
--     anon, authenticated` statement 299 added right after the grant is
--     gone entirely -- not present anywhere in 371's body.
--   * browse_list's district/price covering indexes reverted from
--     migration 283's 9-column form (obec_id, category_type, price_czk,
--     property_id, category_main, subtype, disposition, area_m2,
--     is_active) back to the narrow 3-column form migration 283 replaced --
--     reintroducing the exact heap-fetch performance regression 283 fixed
--     (measured before 283: 12,655 rows removed by filter, 18.9s on the
--     "Domy - Praha" preset).
--
-- Live impact confirmed 2026-08-06: has_table_privilege('anon',
-- 'browse_list', 'SELECT') = true, relrowsecurity = false on both
-- browse_list and properties_map_mv -- i.e. the full active market dataset
-- (514,889+ properties: price, disposition, exact coordinates, locality/
-- street, condition, MF rent/yield, all near_* proximity scores, price-
-- change history) is readable by anyone with the public anon key (which is
-- unavoidably embedded in the shipped frontend JS bundle -- RequireAuth is
-- a React Router mounting gate, not a security boundary; PostgREST grants
-- are the only real one). Since rebuild_browse_list/rebuild_properties_map_mv
-- blue-green DROP+CREATE these objects every 5 / 30 minutes via pg_cron,
-- this has been a continuously-reasserted regression, not stale drift --
-- every tick re-grants anon.
--
-- Root cause of how this shipped: docs/design/phase-0-emergency-hardening.md
-- explicitly warned against exactly this mistake ("DO NOT hand-retype the
-- function. Take migration 283's ... body VERBATIM") and even documents an
-- EMERGENCY ROLLBACK snippet elsewhere in the same doc that intentionally
-- keeps anon SELECT for use only if Browse breaks -- 371 most likely copied
-- from an outdated source (277, or the rollback snippet) instead of the
-- live-correct 299 version.
--
-- Fix, two parts:
--  1. Restore 299's grant (authenticated only) + write-revoke, and 283's
--     covering indexes, for BOTH functions. Bodies otherwise unchanged from
--     371 (the statement_timeout proconfig removal that migration was
--     actually about is correct and stays removed).
--  2. A STANDING self-check: after the grant, each function now asserts
--     `NOT has_table_privilege('anon', ..., 'SELECT')` and RAISEs if it
--     ever fails. This is the fix for the deeper problem the report
--     flagged: migration 331's post-condition assertion runs once, at
--     migration-apply time, inside a transaction -- structurally unable to
--     catch a SCHEDULED FUNCTION regressing on some later edit, since nothing
--     re-runs it. An assertion INSIDE the function itself runs on every
--     single pg_cron tick, forever (every 5 min for browse_list, every 30
--     min for the map) -- the next time anyone reintroduces this mistake, it
--     is caught within one tick, not nine months. A RAISE inside a pg_cron
--     command's implicit transaction rolls back the WHOLE tick, including
--     the DROP/RENAME already done -- so a broken rebuild leaves the last
--     KNOWN-GOOD browse_list/properties_map_mv in place (correctly
--     permissioned) rather than replacing it with a mis-permissioned one,
--     and list_rebuilt_at/map_rebuilt_at stop advancing, which
--     migration 375's Health-page banner surfaces within minutes. This
--     mirrors 299's own Part D rationale ("the durable fix is to re-assert
--     the lock INSIDE each rebuild") applied to the grant instead of the
--     advisory lock.
--
-- A second, OFFLINE layer complements this: tests/test_browse_grant_drift.py
-- (same PR) statically scans the LATEST rebuild_browse_list/
-- rebuild_properties_map_mv definition across all migrations for an
-- EXECUTE-embedded anon grant, catching a future regression at PR review
-- time -- before it ever reaches a pg_cron tick. Neither layer alone is
-- sufficient: the offline scan cannot see a grant issued directly against
-- prod outside a migration file (this incident's proximate cause was a
-- migration, but 299's own header notes a bare default-ACL re-grant has
-- happened before with no migration involved); the runtime check cannot
-- catch anything before the next scheduled tick actually runs.
--
-- Statement_timeout / pg_cron scheduling (371's actual subject) is
-- unchanged -- the `cron.schedule(...)` calls in 371 already re-arm it
-- correctly in the cron command itself and are not touched here.

set local lock_timeout = '5s';

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
    execute 'create index browse_list_next_obec_price_idx on browse_list_next (obec_id, category_type, price_czk, property_id, category_main, subtype, disposition, area_m2, is_active) where obec_id is not null';
    execute 'create index browse_list_next_okres_price_idx on browse_list_next (okres_id, category_type, price_czk, property_id, category_main, subtype, disposition, area_m2, is_active) where okres_id is not null';
    execute 'create index browse_list_next_region_price_idx on browse_list_next (region_id, category_type, price_czk, property_id, category_main, subtype, disposition, area_m2, is_active) where region_id is not null';
    execute 'analyze browse_list_next';
    execute 'select count(*) from browse_list_next' into n;

    execute 'drop table if exists browse_list';
    execute 'alter table browse_list_next rename to browse_list';
    execute 'alter index browse_list_next_pk rename to browse_list_pk';
    execute 'alter index browse_list_next_cat_first_seen_idx rename to browse_list_cat_first_seen_idx';
    execute 'alter index browse_list_next_obec_price_idx rename to browse_list_obec_price_idx';
    execute 'alter index browse_list_next_okres_price_idx rename to browse_list_okres_price_idx';
    execute 'alter index browse_list_next_region_price_idx rename to browse_list_region_price_idx';
    execute 'grant select on browse_list to authenticated';
    execute 'revoke insert, update, delete, truncate on browse_list from anon, authenticated';

    if has_table_privilege('anon', 'browse_list', 'SELECT') then
      raise exception 'rebuild_browse_list: anon must never hold SELECT on browse_list -- refusing to publish this rebuild (see migration 376)';
    end if;

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
    execute 'grant select on properties_map_mv to authenticated';

    if has_table_privilege('anon', 'properties_map_mv', 'SELECT') then
      raise exception 'rebuild_properties_map_mv: anon must never hold SELECT on properties_map_mv -- refusing to publish this rebuild (see migration 376)';
    end if;

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

-- Apply the fix immediately (both objects are currently anon-readable and
-- carry the narrow indexes) rather than waiting up to 5/30 min for the next
-- tick -- ci-allow-dynamic: same EXECUTE-based dynamic DDL as migrations
-- 277/299/363, inherited verbatim, not new here.
select rebuild_browse_list();
select rebuild_properties_map_mv();

-- Embedded post-condition: fail the migration itself if the live grant
-- didn't take (belt-and-braces alongside the in-function checks above,
-- which only fire on the NEXT scheduled tick's re-publish, not retroactively
-- if pg_cron is unavailable in this environment).
do $$
begin
  if to_regclass('public.browse_list') is not null then
    assert not has_table_privilege('anon', 'public.browse_list', 'SELECT'),
      'browse_list is still anon-SELECTable after migration 376';
  end if;
  if to_regclass('public.properties_map_mv') is not null then
    assert not has_table_privilege('anon', 'public.properties_map_mv', 'SELECT'),
      'properties_map_mv is still anon-SELECTable after migration 376';
  end if;
end $$;
