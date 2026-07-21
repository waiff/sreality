-- 332_health_ops_rpc_admin_gate.sql
--
-- Closes the last ungated admin-ops read surface, found while executing the R2
-- grant hardening. Spec: docs/design/public-release-remediation-2026-07.md § R2.
--
-- Migration 318 gated 26 admin-only views/functions behind `is_platform_admin()`,
-- but its triage worked from the Supabase advisor's `security_definer_view` list —
-- so it never saw these five, which are plain `SECURITY INVOKER` SQL functions:
--
--   health_summary()          -> health_summary_mv, health_mv_refresh_stamp
--   portal_health_summary()   -> portal_health_mv
--   scraper_health_checks()   -> scraper_health_checks_mv, health_mv_refresh_stamp
--   category_trends()         -> category_trends_mv
--   image_storage_overview()  -> image_storage_overview_mv
--
-- All five carry `EXECUTE` for `authenticated` and are consumed by exactly one
-- surface, the admin Health dashboard (frontend/src/pages/Health.tsx, route-gated
-- by <AdminPage>/RequireAdmin). Route gating is a client-side affordance, not a
-- security boundary: any signed-in non-admin can call these directly over
-- supabase-js RPC and read full scraper internals — per-source failure/backlog
-- counts, LLM/image storage volumes, delisting latencies. Latent today (the one
-- account IS the admin) and a leak the moment Wave 1 signs up a tenant.
--
-- They are also why migration 331 could NOT simply revoke `authenticated`'s SELECT
-- on the health matviews: an INVOKER function reads them as the CALLER, so the
-- revoke would have broken the operator's own dashboard. The two halves have to
-- move together, which is why this is its own migration:
--   1. each function becomes SECURITY DEFINER (so it reads the matview as its
--      postgres owner) and embeds the gate, exactly like migration 318's objects;
--   2. `authenticated` then loses SELECT on the seven matviews they wrap, so the
--      raw-matview bypass closes too (a matview can carry neither RLS nor a gate).
--
-- Non-admins get NULL rather than a permission error — a clean deny, and the same
-- shape a caller already handles when a matview has not been refreshed yet. The
-- gate is a standalone pseudoconstant qual, so the planner evaluates it once as a
-- One-Time Filter and skips the read entirely for a non-admin.
--
-- Signatures (including DEFAULTs) are reproduced exactly from pg_get_functiondef:
-- the SPA calls category_trends and scraper_health_checks with p_source only, so
-- dropping a default would break the dashboard.

begin;

create or replace function public.health_summary()
returns jsonb
language sql
stable
security definer
set search_path to 'public'
as $function$
  select case when is_platform_admin() then (
    select payload || jsonb_build_object(
      'generated_at', (select refreshed_at from health_mv_refresh_stamp)
    )
    from health_summary_mv
    limit 1
  ) end;
$function$;

create or replace function public.portal_health_summary()
returns jsonb
language sql
stable
security definer
set search_path to 'public'
as $function$
  select case when is_platform_admin() then (
    select payload from portal_health_mv limit 1
  ) end;
$function$;

create or replace function public.scraper_health_checks(p_source text default 'sreality'::text)
returns jsonb
language sql
stable
security definer
set search_path to 'public'
as $function$
  select case when is_platform_admin() then (
    select payload || jsonb_build_object(
      'generated_at', (select refreshed_at from health_mv_refresh_stamp)
    )
    from scraper_health_checks_mv
    where source = p_source
    limit 1
  ) end;
$function$;

create or replace function public.category_trends(
  p_source text default 'sreality'::text,
  p_hours integer default 72,
  p_days integer default 30
)
returns jsonb
language sql
stable
security definer
set search_path to 'public'
as $function$
  select case when is_platform_admin() then
    coalesce(
      (select payload from category_trends_mv where source = p_source limit 1),
      '[]'::jsonb
    )
  end;
$function$;

create or replace function public.image_storage_overview()
returns jsonb
language sql
stable
security definer
set search_path to 'public'
as $function$
  select case when is_platform_admin() then (
    select jsonb_build_object(
      'total_images',         coalesce(sum(total), 0),
      'stored_images',        coalesce(sum(stored), 0),
      'total_active_images',  coalesce(sum(total_active), 0),
      'stored_active_images', coalesce(sum(stored_active), 0),
      'by_category', coalesce(
        jsonb_agg(
          jsonb_build_object(
            'category_main', category_main,
            'category_type', category_type,
            'total',         total,
            'stored',        stored,
            'total_active',  total_active,
            'stored_active', stored_active
          )
          order by category_main, category_type
        ),
        '[]'::jsonb)
    )
    from image_storage_overview_mv
  ) end;
$function$;

-- This project's default ACL re-grants EXECUTE on a recreated function to anon +
-- authenticated (migration 287), and a bare `revoke from public` does not remove an
-- explicit grant — so re-assert the intended posture rather than assume it survived.
revoke execute on function public.health_summary() from public, anon;
revoke execute on function public.portal_health_summary() from public, anon;
revoke execute on function public.scraper_health_checks(text) from public, anon;
revoke execute on function public.category_trends(text, integer, integer) from public, anon;
revoke execute on function public.image_storage_overview() from public, anon;

grant execute on function public.health_summary() to authenticated;
grant execute on function public.portal_health_summary() to authenticated;
grant execute on function public.scraper_health_checks(text) to authenticated;
grant execute on function public.category_trends(text, integer, integer) to authenticated;
grant execute on function public.image_storage_overview() to authenticated;

-- Now that every reader is DEFINER-owned, the raw matviews go dark to the browser
-- roles. `properties_map_mv` / `price_stat_choropleth` / `rent_map_choropleth` are
-- deliberately untouched: the SPA reads those three directly as shared-market data.
revoke select on
  public.health_summary_mv,
  public.health_mv_refresh_stamp,
  public.portal_health_mv,
  public.scraper_health_checks_mv,
  public.snapshot_churn_24h_mv,
  public.category_trends_mv,
  public.image_storage_overview_mv
from anon, authenticated;

do $$
declare
  v_leaky text[];
  v_ungated text[];
begin
  select coalesce(array_agg(x order by x), '{}') into v_leaky
    from unnest(array[
      'health_summary_mv', 'health_mv_refresh_stamp', 'portal_health_mv',
      'scraper_health_checks_mv', 'snapshot_churn_24h_mv', 'category_trends_mv',
      'image_storage_overview_mv'
    ]) as x
   where has_table_privilege('authenticated', ('public.' || x)::regclass, 'SELECT');
  if array_length(v_leaky, 1) is not null then
    raise exception 'ops matview(s) still readable by authenticated: %', v_leaky;
  end if;

  select coalesce(array_agg(p.proname::text order by p.proname), '{}') into v_ungated
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
   where n.nspname = 'public'
     and p.proname in ('health_summary', 'portal_health_summary', 'scraper_health_checks',
                       'category_trends', 'image_storage_overview')
     and (not p.prosecdef or p.prosrc not like '%is_platform_admin()%');
  if array_length(v_ungated, 1) is not null then
    raise exception 'health RPC(s) not SECURITY DEFINER + gated: %', v_ungated;
  end if;
end $$;

commit;
