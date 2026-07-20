-- 330_admin_gate_fallback_tighten.sql
--
-- Tightens migration 329's claims-absent fallback in is_platform_admin().
--
-- 329 opens the gate when there is no JWT context and `session_user` is a
-- bypassrls LOGIN role (pg_cron, the SUPABASE_DB_URL scripts). Live testing of
-- 329 found that guard is looser than intended: `SET ROLE` does not change
-- session_user, so on a connection that logs in as an owner/bypassrls role and
-- then simulates a browser role — `SET LOCAL ROLE authenticated` with no
-- claims — the fallback still fired and reported admin.
--
-- Production is unaffected either way (PostgREST logs in as `authenticator` and
-- the API's per-account pool as `tenant_pool`, neither of which is bypassrls, so
-- both already failed closed). The exposure is (a) the CI schema-replay DB, whose
-- login IS the table owner, where a role-switch simulation would have been
-- silently over-privileged and could mask a real gate regression, and (b) any
-- future direct-connection code that switches role to act on a tenant's behalf.
--
-- Fix: require that no `SET ROLE` is in effect. The `role` GUC is NOT masked by
-- SECURITY DEFINER (unlike current_user, which is the definer inside this body),
-- so it reads 'none' for a genuine service connection and the switched-to role
-- name after any SET ROLE. Both `session_user` and the role-GUC check must pass.
--
-- Behaviour-preserving for every real consumer: pg_cron invokes
-- refresh_health_matviews() with no SET ROLE (SECURITY DEFINER does not set the
-- role GUC) and the service-role scripts connect without one, so both keep the
-- fallback. Verified live before and after.

begin;

create or replace function public.is_platform_admin()
returns boolean
language sql
stable
security definer
set search_path to 'public'
as $function$
  select case
    when nullif(current_setting('request.jwt.claims', true), '') is null then
      -- No JWT context => not a PostgREST or tenant-pool request. Trust only a
      -- genuine direct service connection: a bypassrls LOGIN role with no role
      -- switch in effect. session_user (not current_user, which is the definer
      -- here) identifies the login; the role GUC catches a simulated browser role.
      coalesce(current_setting('role', true), 'none') = 'none'
      and coalesce(
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

do $$
declare
  v_direct boolean;
  v_switched boolean;
begin
  -- A claims-less service connection still opens the gate (pg_cron + scripts).
  select public.is_platform_admin() into v_direct;
  if not v_direct then
    raise exception 'claims-less service connection lost admin — pg_cron/scripts would break';
  end if;

  -- ... but a claims-less role switch on that same connection does not.
  set local role authenticated;
  select public.is_platform_admin() into v_switched;
  reset role;
  if v_switched then
    raise exception 'SET ROLE authenticated with no claims still reports admin';
  end if;
end $$;

commit;
