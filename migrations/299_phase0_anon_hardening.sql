-- 299_phase0_anon_hardening.sql
-- PHASE 0 EMERGENCY HARDENING — close the 3 live anon-exploitable criticals, now
-- reconciled with the SHIPPED Phase-1 posture (login-gated SPA, tenant RLS live).
--
-- Root cause (unfixed until now): Supabase's public-schema DEFAULT ACL grants EVERY
-- privilege to anon+authenticated on every EXISTING and FUTURE object owned by
-- postgres — invisible to the 300+ migrations. Live proof it is active: migration
-- 294 revoked anon EXECUTE on seed_default_pipeline/seed_default_collections/
-- backfill_legacy_account_id, yet a later migration re-created them and the default
-- ACL RE-GRANTED anon EXECUTE (they are anon-executable again today).
--
-- Reconciliation with Phase 1 (design doc phase-0 predates the login gate):
--   * The SPA is FULLY login-gated (RequireAuth wraps the whole Shell) and does ZERO
--     supabase-js writes, so its reads now run as `authenticated`, never `anon`.
--     Nothing reads as anon (verified: no supabase.from/rpc outside RequireAuth).
--     => anon is revoked to ~nothing (SELECT included), not merely its writes.
--   * The shared-market surfaces the SPA reads already GRANT `authenticated` SELECT,
--     so there is no anon->authenticated "re-grant" to do — only an anon revoke.
--   * EXCEPTION (Amendment A6, operator-confirmed 2026-07-12 "dark to all now"):
--     the broker-directory PII surfaces stay dark to `authenticated` too, until
--     Wave 4 ships masked columns. Encoded as a live CI assertion (tenant-isolation
--     lane) + an offline re-grant guard (test_migration_rls_grants.py).
--   * The 19 user-state TENANT tables (migrations 290-294,298) keep their deliberate
--     `authenticated` DML grants — RLS + current_account_ids() scopes them per tenant.
--     They are EXCLUDED from the authenticated-write revoke below.
--
-- Safe on the live system: the backend/worker/63 workflows connect as postgres/
-- service_role (BYPASSRLS), provable because `listings` has carried RLS-with-no-policy
-- since migration 001 yet is UPSERTed hourly. Enabling RLS on more internal tables
-- and revoking browser-role grants cannot block a BYPASSRLS writer.
--
-- See docs/design/phase-0-emergency-hardening.md (+ phase-1 §Settled decisions, A5/A6).
-- One transaction: a mid-apply failure rolls back cleanly. Dead-backup-table DROPs
-- are deliberately NOT here (destructive → their own migration after a pg_dump).

begin;

-- ============================================================================
-- PART A — ROOT CAUSE: stop the postgres default ACL re-granting future objects.
-- Only affects objects created AFTER this runs; PART B fixes existing ones.
-- We run as `postgres` (owner of this default ACL), so these take effect.
-- Model: anon gets NOTHING ever; authenticated gets READ by default (SELECT on
-- tables, EXECUTE on functions — the login-gated read model), never WRITE and no
-- sequence privileges (write + sequence-usage are granted explicitly per tenant
-- table). A new base table must still `enable row level security` (CI-enforced),
-- so a default SELECT grant is backstopped by deny-all RLS.
-- ============================================================================
alter default privileges for role postgres in schema public revoke all on tables from anon;
alter default privileges for role postgres in schema public revoke all on sequences from anon;
alter default privileges for role postgres in schema public revoke all on functions from anon;
alter default privileges for role postgres in schema public revoke all on sequences from authenticated;
alter default privileges for role postgres in schema public
  revoke insert, update, delete, truncate, references, trigger on tables from authenticated;
-- Functions carry a built-in EXECUTE grant to PUBLIC (anon inherits it), which the
-- anon revoke above does NOT remove — so also revoke the PUBLIC default. Future
-- functions then reach nobody-but-explicitly-granted (authenticated keeps its own
-- default-ACL EXECUTE; anon/PUBLIC get nothing). This is the function-side root cause.
alter default privileges for role postgres in schema public revoke execute on functions from public;

-- ============================================================================
-- PART B — revoke on EXISTING objects.
-- B1: anon loses EVERYTHING (tables, views, matviews, sequences, functions).
-- ============================================================================
revoke all privileges on all tables    in schema public from anon;   -- tables + views
revoke all privileges on all sequences in schema public from anon;

-- Matviews are NOT covered by "ALL TABLES IN SCHEMA" — revoke explicitly.
do $$
declare r record;
begin
  for r in
    select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind = 'm'
  loop
    execute format('revoke all on public.%I from anon', r.relname);
  end loop;
end $$;

-- Functions: revoke EXECUTE from PUBLIC + anon on POSTGRES-owned functions.
-- PUBLIC must go too — every function has a built-in `=X` (PUBLIC) grant that anon
-- inherits, so revoking anon alone leaves it reachable (this is what rolled back the
-- first apply). authenticated keeps its OWN explicit default-ACL grant, so the SPA's
-- read RPCs still work; service_role + postgres keep theirs. The 118 supabase_admin
-- extension functions (PostGIS, pg_trgm, …) are harmless computational helpers we
-- neither own nor need to touch (postgres is not superuser, so it can't revoke them).
do $$
declare r record;
begin
  for r in
    select p.oid::regprocedure as sig
    from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and pg_get_userbyid(p.proowner) = 'postgres'
  loop
    execute format('revoke all on function %s from public, anon', r.sig);
  end loop;
end $$;

-- ============================================================================
-- B2: authenticated loses WRITE on every table + view EXCEPT the 19 tenant
-- tables (which hold deliberate, RLS-scoped DML grants from migrations 290-294,298).
-- SELECT + EXECUTE are UNTOUCHED here (the login-gated read model). This closes
-- critical #2 (write-through the auto-updatable *_public views) for authenticated
-- too, not just anon — a logged-in user must not tamper with shared market data.
-- ============================================================================
do $$
declare
  r record;
  tenant_tables text[] := array[
    'collections','tags','property_notes','filter_presets','notification_subscriptions',
    'manual_rental_estimates','collection_properties','property_tags','notification_dispatches',
    'estimation_cohort_entries','estimation_trace_payloads','estimation_feedback',
    'building_run_attachments','estimation_runs','building_runs','property_pipeline',
    'pipeline_stages','property_pipeline_events','entitlements'];
begin
  for r in
    select c.relname, c.relkind
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public'
      and c.relkind in ('r','p','v')          -- base tables, partitioned, views
      and not (c.relname = any(tenant_tables))
  loop
    if r.relkind = 'v' then
      -- views take only insert/update/delete (the auto-updatable write vector)
      execute format('revoke insert, update, delete on public.%I from authenticated', r.relname);
    else
      execute format('revoke insert, update, delete, truncate, references, trigger on public.%I from authenticated', r.relname);
    end if;
  end loop;
end $$;

-- ============================================================================
-- PART C — RLS-enable (deny-all) the 25 internal RLS-off base tables. RLS-on +
-- no policy = deny-all to anon/authenticated; postgres + service_role bypass.
-- Metadata-only, instant even on address_points (1.56M rows). browse_list is
-- intentionally EXCLUDED (blue-green rebuilt every 5 min → handled in PART D).
-- Also strip any residual authenticated grant (RLS already denies; belt+braces).
-- ============================================================================
do $$
declare
  t text;
  rls_off_tables text[] := array[
    -- dead backup tables (a later migration DROPs them after a pg_dump)
    '_backup_estimation_subject_summary_20260602','_bazos_mistagged_20260602',
    'dq_p0_backfill_backup','images_backfill_backup_20260609',
    'notification_dispatches_pre204_backup','placeholder_backfill_backup_20260612',
    -- internal / operational
    'address_points','address_points_revisions','broker_merge_candidates',
    'broker_outreach_suppression','broker_resolution_lock','data_quality_snapshots',
    'dedup_geo_scan_state','dedup_golden_pairs','dedup_pair_audit','filter_visibility',
    'legacy_backfill_claim','listing_description_enrichments','outreach_campaigns',
    'outreach_messages','portal_limits_history','price_stat_locality_no_data',
    'property_identity_candidates_archive','workflow_failures','workflow_run_health'];
begin
  foreach t in array rls_off_tables loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on public.%I from anon, authenticated', t);
  end loop;
end $$;

-- ============================================================================
-- PART D — browse_list + properties_map_mv: keep them readable by `authenticated`
-- (the SPA's Browse list + map read them directly), dark to anon, unwritable by
-- either browser role. Both are rebuilt blue-green every 5 min by a SECURITY
-- DEFINER function owned by a BYPASSRLS role (postgres) — verified — so the
-- durable fix is to re-assert the lock INSIDE each rebuild (else the next tick
-- re-creates the object RLS-off and the default ACL re-grants anon). We also lock
-- the CURRENT objects now so there is no open window before the next rebuild.
-- browse_list keeps the Supabase rls_disabled_in_public advisor (cosmetic — writes
-- are locked every cycle; RLS on a table dropped/renamed every 5 min is moot).
-- ============================================================================
revoke all on public.browse_list from anon;
revoke insert, update, delete, truncate on public.browse_list from authenticated;
revoke all on public.properties_map_mv from anon;

-- rebuild_browse_list(): migration 283's body verbatim, with the anon SELECT grant
-- narrowed to authenticated and a write-revoke added right after it.
create or replace function rebuild_browse_list()
returns void
language plpgsql
security definer
set search_path = public
set statement_timeout = '600s'
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
    -- PHASE 0: authenticated-only reads (login-gated SPA), never anon; no browser writes.
    execute 'grant select on browse_list to authenticated';
    execute 'revoke insert, update, delete, truncate on browse_list from anon, authenticated';

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

-- rebuild_properties_map_mv(): same narrowing (a matview can't be DML'd, so only
-- the anon SELECT grant needs dropping). Body otherwise verbatim from live.
create or replace function rebuild_properties_map_mv()
returns void
language plpgsql
security definer
set search_path = public
set statement_timeout = '600s'
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
    -- PHASE 0: authenticated-only reads (login-gated SPA), never anon.
    execute 'grant select on properties_map_mv to authenticated';

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

-- ============================================================================
-- PART E — lock the dangerous SECURITY DEFINER functions from BOTH browser roles.
-- anon EXECUTE was already stripped in B1; this adds authenticated (a logged-in
-- user must not trigger a full browse_list/matview rebuild [DoS], refresh health
-- matviews [DoS], emit a definer alert-write, or run the tenant seeders/backfill).
-- ============================================================================
do $$
declare r record;
begin
  for r in
    select p.oid::regprocedure as sig
    from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname in (
      'rebuild_browse_list','rebuild_properties_map_mv','refresh_health_matviews',
      'emit_verification_stale_alert','publication_gate_enabled',
      'backfill_legacy_account_id','seed_default_collections','seed_default_pipeline')
  loop
    -- PUBLIC too (else authenticated/anon inherit EXECUTE via the built-in grant)
    execute format('revoke all on function %s from public, anon, authenticated', r.sig);
  end loop;
end $$;

-- ============================================================================
-- PART F — Amendment A6: keep the broker-directory PII surfaces DARK to
-- `authenticated` until Wave 4 masks them (operator-confirmed 2026-07-12).
-- The broker BASE tables (brokers, broker_identities, …) are already RLS-on with
-- no policy, so authenticated cannot read them directly; the SPA reaches them only
-- through these SECURITY DEFINER *_public views/matview/function — so revoking the
-- browser roles here is sufficient to close the ~17.8k-email/~23.4k-phone surface.
-- anon was already revoked in PART B; this adds authenticated.
-- (brokers_public.primary_email/primary_phone is the aggregated broker contact DB.
--  The per-listing advertised agent contact on listings_public/properties_public is
--  a distinct, lesser exposure handled holistically by the Wave-4 DPIA — NOT here.)
-- ============================================================================
revoke all on public.brokers_public                 from anon, authenticated;
revoke all on public.broker_firm_memberships_public from anon, authenticated;
revoke all on public.broker_listings_public         from anon, authenticated;
revoke all on public.listing_broker_public          from anon, authenticated;
revoke all on public.broker_geo_options             from anon, authenticated;
revoke all on public.broker_resolution_runs_public  from anon, authenticated;
revoke all on public.broker_region_type_stats       from anon, authenticated;   -- matview
revoke all on function public.broker_leaderboard(bigint[],bigint[],bigint[],text,text,text,integer)
  from public, anon, authenticated;   -- PUBLIC too, else roles inherit EXECUTE

-- ============================================================================
-- PART G — embedded post-conditions: fail (and roll back) the whole migration if
-- any critical invariant did not take. Cheaper than discovering it live.
-- ============================================================================
do $$
begin
  -- anon is dark everywhere
  assert not has_table_privilege('anon','public.address_points','SELECT'),
         'anon still SELECTs address_points — PART B1 did not take';
  assert not has_table_privilege('anon','public.listings','SELECT'),
         'anon still SELECTs listings — PART B1 did not take';
  assert not has_table_privilege('anon','public.browse_list','SELECT'),
         'anon still SELECTs browse_list — PART D did not take';
  assert not has_table_privilege('anon','public.listings_public','SELECT'),
         'anon still SELECTs the listings_public view — PART B1 did not take';
  assert not has_function_privilege('anon','public.refresh_health_matviews()','EXECUTE'),
         'anon still EXECUTEs refresh_health_matviews — PART B1/E (PUBLIC) did not take';
  assert not has_function_privilege('anon','public.health_summary()','EXECUTE'),
         'anon still EXECUTEs health_summary — PART B1 PUBLIC revoke did not take';

  -- authenticated keeps the login-gated read model on shared market surfaces
  assert has_table_privilege('authenticated','public.browse_list','SELECT'),
         'authenticated LOST browse_list SELECT — Browse would break, aborting';
  assert has_table_privilege('authenticated','public.listings_public','SELECT'),
         'authenticated LOST listings_public SELECT — the SPA would break, aborting';
  assert has_table_privilege('authenticated','public.properties_map_mv','SELECT'),
         'authenticated LOST properties_map_mv SELECT — the map would break, aborting';
  -- the SPA read RPCs must survive the PUBLIC revoke (they keep an explicit grant)
  assert has_function_privilege('authenticated','public.health_summary()','EXECUTE'),
         'authenticated LOST health_summary EXECUTE — Health page would break, aborting';
  assert has_function_privilege('authenticated','public.images_failure_overview()','EXECUTE'),
         'authenticated LOST images_failure_overview EXECUTE — Health page would break, aborting';

  -- authenticated write is closed on shared data, kept on tenant tables
  assert not has_table_privilege('authenticated','public.listings','INSERT'),
         'authenticated still INSERTs listings — PART B2 did not take';
  assert not has_table_privilege('authenticated','public.listings_public','INSERT'),
         'authenticated still INSERTs through listings_public — PART B2 did not take';
  assert has_table_privilege('authenticated','public.collections','INSERT'),
         'authenticated LOST collections INSERT — tenant write path broken, aborting';
  assert has_table_privilege('authenticated','public.property_pipeline','UPDATE'),
         'authenticated LOST property_pipeline UPDATE — tenant write path broken, aborting';

  -- A6: broker PII dark to authenticated
  assert not has_table_privilege('authenticated','public.brokers_public','SELECT'),
         'authenticated can still read brokers_public (broker PII) — A6 not enforced';
  assert not has_function_privilege('authenticated',
         'public.broker_leaderboard(bigint[],bigint[],bigint[],text,text,text,integer)','EXECUTE'),
         'authenticated can still EXECUTE broker_leaderboard — A6 not enforced';

  -- dangerous definer functions locked from both browser roles
  assert not has_function_privilege('authenticated','public.backfill_legacy_account_id(uuid)','EXECUTE'),
         'authenticated can still EXECUTE backfill_legacy_account_id — PART E did not take';
end $$;

commit;
