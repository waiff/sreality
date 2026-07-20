-- 329_admin_gate_p0_hotfix.sql
--
-- Fixes three live regressions shipped by migrations 316 + 318. Plan + full
-- live evidence: docs/design/public-release-remediation-2026-07.md § R1.
--
-- PART A — migration 316 flipped `security_invoker = true` on eight views it
-- called "per-account tenant views". Seven of them are; `property_estimates_public`
-- is NOT. It is a market-wide aggregate (migration 173, re-created in 311) that
-- INNER JOINs `listings`, and `listings` carries RLS-enabled-with-ZERO-policies
-- so that its owner-rights views (listings_public, properties_public, ...) can
-- serve shared-market reads. Under invoker rights that join is deny-all for
-- every non-bypassrls role: `authenticated` gets 0 rows, `anon` gets a hard
-- 42501. That silently empties BOTH surfaces the view backs — Browse's
-- "with estimates" prefilter (resolveEstimatesPrefilter -> zero property_ids ->
-- zero cards/rows/map pins) and the Stats RPC (browse_stats_properties is
-- SECURITY INVOKER and tests `EXISTS (select 1 from property_estimates_public …)`).
-- Reverting the one view heals both; no application change is needed.
--
-- PART B — is_platform_admin() keys solely on the `request.jwt.claims` GUC, which
-- only PostgREST/supabase-js and the tenant pool ever set. Migration 318 embedded
-- that function as a row filter in 26 admin-ops views/functions, so on any direct
-- connection the gate reads false and those objects return zero rows. Two live
-- consumers broke silently:
--   * scripts/build_dedup_golden_set.py reads dedup_label_events over raw psycopg
--     -> freezes 0 rows, logs success, exits 0 (indistinguishable from a no-op
--     re-run), which in turn leaves validate_vision_models.py benchmarking a
--     stale set.
--   * the */10 pg_cron refresh_health_matviews() rebuilds three matviews whose
--     bodies read now-gated views -> the Health dashboard has been reporting
--     "0 active failures" / "0 queued" / null parse-activity while the base
--     tables hold 1485 fetch-failure and 895 queue rows. That is exactly the
--     failure-tracking signal architectural rule #5 depends on.
--
-- The fallback below fires ONLY when there is no JWT context at all, and keys on
-- `session_user`. It must not key on `current_user`: inside a SECURITY DEFINER
-- body current_user is the DEFINER (postgres) for every caller, so a
-- current_user-based guard would return true for anon and open all 26 objects to
-- the world. session_user is not masked by SECURITY DEFINER — it stays the real
-- login role: postgres for pg_cron and the SUPABASE_DB_URL scripts, `authenticator`
-- for PostgREST, `tenant_pool` for the API's per-account pool. Only bypassrls
-- login roles (postgres / service_role / supabase_admin) pass, so every browser
-- and tenant path fails closed — and the tenant pool always sets claims before it
-- issues a query (api/tenant_pool.py), so the fallback is unreachable there even
-- in principle.
--
-- Additive/behavioural only: no table, column, policy, or grant is touched.

begin;

-- PART A ---------------------------------------------------------------------
alter view public.property_estimates_public set (security_invoker = false);

-- PART B ---------------------------------------------------------------------
create or replace function public.is_platform_admin()
returns boolean
language sql
stable
security definer
set search_path to 'public'
as $function$
  select case
    when nullif(current_setting('request.jwt.claims', true), '') is null then
      -- No JWT context => not a PostgREST or tenant-pool request. Trust only
      -- bypassrls LOGIN roles (postgres / service_role / supabase_admin): that is
      -- pg_cron and the service-role scripts. session_user is deliberate — see
      -- the header; current_user would be the definer here and open the gate.
      coalesce(
        (select r.rolbypassrls from pg_roles r where r.rolname = session_user),
        false
      )
    else exists (
      select 1 from admins a
      where a.user_id = nullif(
        current_setting('request.jwt.claims', true)::jsonb ->> 'sub', ''
      )::uuid
    )
  end
$function$;

-- Post-conditions: fail loudly rather than leave a half-fixed gate live.
--
-- Deliberately NOT asserted here: "the view returns > 0 rows". This block runs as
-- the migration's superuser, which bypasses RLS, so it would read rows even with
-- the broken `security_invoker = true` setting — it cannot detect the bug it looks
-- like it is testing, and it hard-fails the CI schema replay, whose database has no
-- estimation_runs data at all. The reloption assertion below is the real guard; the
-- behavioural check belongs in the live test lane under a non-privileged role
-- (tests/test_tenant_isolation_live.py::test_market_view_readable_by_authenticated).
do $$
declare
  v_invoker text;
  v_admin boolean;
begin
  select coalesce(
           (select option_value
              from pg_options_to_table(c.reloptions)
             where option_name = 'security_invoker'),
           'false')
    into v_invoker
    from pg_class c
   where c.oid = 'public.property_estimates_public'::regclass;
  if v_invoker <> 'false' then
    raise exception 'property_estimates_public still security_invoker=%', v_invoker;
  end if;

  -- This session has no JWT claims and is a bypassrls role, so the fallback must
  -- open the gate and the gated view must return its rows again.
  select public.is_platform_admin() into v_admin;
  if not v_admin then
    raise exception 'is_platform_admin() still false on a claims-less bypassrls session';
  end if;

  -- Data-independent: the view must at least be queryable end to end (a broken
  -- definition or a missing grant on a base table raises here).
  perform 1 from public.property_estimates_public limit 1;
end $$;

commit;
