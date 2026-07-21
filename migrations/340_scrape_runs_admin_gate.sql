-- 340_scrape_runs_admin_gate.sql
--
-- Closes the last ungated admin-ops read surface, found by the exit-gate audit of
-- the 329-332 batch. Plan: docs/design/public-release-remediation-round2.md § PR-A.
--
-- `scrape_runs_public` is a plain 14-column projection of `scrape_runs` and
-- `recent_scrape_runs()` is a SECURITY INVOKER wrapper over it. Both carry EXECUTE/
-- SELECT for `authenticated`, and the base table's RLS (enabled, zero policies) is
-- defeated because the view is owner-rights. Verified live 2026-07-21 under
-- `set local role authenticated`: 7945 rows through the view, 2166 through the RPC
-- -- per-run new/updated/inactive listing counts, image volumes, error blobs and
-- per-category breakdowns. The only consumer is the admin Health dashboard
-- (frontend/src/lib/queries.ts -> Health.tsx), which is route-gated by
-- <AdminPage>/RequireAdmin -- a client affordance, not a security boundary: any
-- signed-in non-admin can call this directly over supabase-js.
--
-- This is exactly the class migration 332 closed for five health/ops RPCs. The
-- standing gate did not catch it because `scrape_runs` was absent from
-- _ADMIN_ONLY_RELATIONS -- that list was seeded from migration 318's objects rather
-- than a first-principles table inventory. A full inventory has now been run; it
-- found one further sibling, `worker_liveness` over `worker_heartbeats`, which is
-- NOT browser-readable today (no anon/authenticated grant) but is the same class, so
-- it is gated here too and both base tables join the sensitive list in the same PR.
--
-- `recent_scrape_runs` is deliberately repointed at the BASE table rather than the
-- gated view: reading the wrapper would gate it transitively but leave it invisible
-- to the standing function-sweep, i.e. a permanent blind spot.
--
-- Additive/permission-only: no table, column or policy changes.

begin;

-- Fail fast rather than queue behind (or ahead of) a pg_cron refresh holding a
-- conflicting lock -- GRANT/REVOKE and CREATE OR REPLACE take ACCESS EXCLUSIVE.
set local lock_timeout = '5s';

-- Migration 318's view-wrapper pattern. Body reproduced verbatim from
-- pg_get_viewdef; the gate, not RLS/security_invoker, is the boundary.
create or replace view public.scrape_runs_public as
select * from (
  select
    id,
    started_at,
    ended_at,
    run_type,
    index_pages,
    listings_found_new,
    listings_scraped_new,
    listings_updated,
    listings_inactive,
    images_discovered,
    images_stored,
    errors,
    by_category,
    source
  from scrape_runs
) __admin_gate
where is_platform_admin();

create or replace view public.worker_liveness as
select * from (
  select
    worker,
    beat_at,
    extract(epoch from now() - beat_at)::integer as age_seconds
  from worker_heartbeats
) __admin_gate
where is_platform_admin();

-- Migration 332's RPC pattern: SECURITY DEFINER + the gate in a WHERE position.
-- Signature incl. the DEFAULT is reproduced exactly from pg_get_functiondef -- the
-- SPA calls recent_scrape_runs({p_days}), so dropping the default would break it.
create or replace function public.recent_scrape_runs(p_days integer default 14)
returns setof scrape_runs
language sql
stable
security definer
set search_path to 'public'
as $function$
  select *
  from scrape_runs
  where started_at > now() - make_interval(days => p_days)
    and is_platform_admin()
  order by started_at desc
$function$;

-- This project's default ACL re-grants EXECUTE on a recreated function to anon and
-- authenticated (migration 287), and `revoke from public` does not remove an
-- explicit grant -- so re-assert the intended posture instead of assuming it
-- survived. Views keep their existing (anon-less) ACL across CREATE OR REPLACE.
revoke execute on function public.recent_scrape_runs(integer) from public, anon;
grant execute on function public.recent_scrape_runs(integer) to authenticated;

-- Record the grant production already holds. `authenticated` can SELECT
-- scrape_runs_public live (that IS the exposure this migration gates), but no
-- migration ever wrote that grant down -- it came from this project's default ACL at
-- view-creation time, which a vanilla replay does not reproduce. Migration 319 did
-- exactly this for the other RLS-gated views; scrape_runs_public was missed then for
-- the same reason it was missed by the gate. Stating it keeps repo and live in step
-- and lets the deny test read the view. Now that the gate is embedded, the grant
-- yields zero rows to a non-admin.
grant select on public.scrape_runs_public to authenticated;

-- Post-conditions: data-independent (the CI schema replay is empty of business data,
-- and this block runs as a bypassrls superuser whose claims-less session opens the
-- gate -- so any row-count assertion would be vacuous there and misleading here).
do $$
declare
  v_ungated text[];
begin
  select coalesce(array_agg(x order by x), '{}') into v_ungated
    from unnest(array['scrape_runs_public', 'worker_liveness']) as x
   where pg_get_viewdef(('public.' || x)::regclass, true) not like '%is_platform_admin()%';
  if array_length(v_ungated, 1) is not null then
    raise exception 'view(s) not gated: %', v_ungated;
  end if;

  if exists (
    select 1 from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
   where n.nspname = 'public'
     and p.proname = 'recent_scrape_runs'
     and (not p.prosecdef or p.prosrc not like '%is_platform_admin()%')
  ) then
    raise exception 'recent_scrape_runs is not SECURITY DEFINER + gated';
  end if;

  if has_table_privilege('anon', 'public.scrape_runs_public', 'SELECT')
     or has_function_privilege('anon', 'public.recent_scrape_runs(integer)', 'EXECUTE') then
    raise exception 'anon still holds scrape_runs read access';
  end if;

  -- End-to-end queryability: a broken body or a missing base grant raises here.
  perform 1 from public.scrape_runs_public limit 1;
  perform 1 from public.worker_liveness limit 1;
  perform 1 from public.recent_scrape_runs(1) limit 1;
end $$;

commit;
