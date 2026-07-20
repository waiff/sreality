-- 316: close a cross-tenant read leak on every SPA-facing tenant view.
--
-- Phase 1's settled read model (docs/design/phase-1-multitenancy-foundations.md
-- Settled decisions) is: "own user-state via security_invoker views (RLS)". It
-- was never actually built that way -- none of migrations 022/025/202/203/205/
-- 211/278 (the views below) ever set `security_invoker`. A Postgres view with
-- that option unset runs with the permissions of its OWNER (postgres, which
-- BYPASSES RLS), not the querying role, no matter what RLS policies exist on
-- the underlying table. Confirmed live: all 8 views are owned by `postgres`
-- with security_invoker=false, so any `authenticated` SPA session reading them
-- gets EVERY account's rows, not just its own.
--
-- Invisible today only because there is exactly one account. The existing
-- 2-account pen-test (tests/test_tenant_isolation_live.py) never caught this
-- because it asserts RLS on the BASE tables directly (`SET LOCAL ROLE
-- authenticated; SELECT * FROM collections`), which correctly enforces RLS --
-- the SPA never queries base tables, only these _public views. This migration
-- flips the option on the exact 8 views the frontend reads directly via
-- supabase-js (grep: frontend/src/lib/*.ts `.from('..._public')`); a
-- companion test now exercises the view path, not just the base table.
--
-- Safe / non-breaking: RLS policies already scope every one of these tables to
-- `account_id IN (current_account_ids())` (migrations 290-294/298); flipping
-- security_invoker only makes the view start HONORING that policy instead of
-- bypassing it. For today's single account, every row already belongs to that
-- account, so the visible result set is unchanged. current_account_ids() reads
-- request.jwt.claims (a GUC), not the executing role, so it resolves
-- identically whether the view runs as invoker or as owner.

begin;

alter view public.collection_properties_public set (security_invoker = true);
alter view public.collections_public           set (security_invoker = true);
alter view public.pipeline_stages_public        set (security_invoker = true);
alter view public.property_estimates_public     set (security_invoker = true);
alter view public.property_notes_public         set (security_invoker = true);
alter view public.property_pipeline_public      set (security_invoker = true);
alter view public.property_tags_public          set (security_invoker = true);
alter view public.tags_public                   set (security_invoker = true);

do $$
declare v text; missing text[] := '{}';
begin
  foreach v in array array[
    'collection_properties_public','collections_public','pipeline_stages_public',
    'property_estimates_public','property_notes_public','property_pipeline_public',
    'property_tags_public','tags_public'
  ]
  loop
    if not exists (
      select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
      join pg_options_to_table(c.reloptions) o on o.option_name = 'security_invoker'
      where n.nspname = 'public' and c.relname = v and o.option_value = 'true'
    ) then
      missing := missing || v;
    end if;
  end loop;
  assert missing = '{}', 'security_invoker did not take on: ' || array_to_string(missing, ', ');
end $$;

commit;
