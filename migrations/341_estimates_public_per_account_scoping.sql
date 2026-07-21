-- 341_estimates_public_per_account_scoping.sql
--
-- Closes the cross-tenant leak the exit-gate audit found in migration 329.
-- Plan: docs/design/public-release-remediation-round2.md § PR-B.
--
-- `property_estimates_public` aggregates `estimation_runs` (which carries per-account
-- RLS from migration 291) joined to `listings`. Migration 316 wrongly made it
-- security_invoker, which made its INNER JOIN to the zero-policy `listings` table
-- deny-all and returned 0 rows to everyone -- emptying Browse's "with estimates"
-- filter. Migration 329 reverted it to owner rights, which fixed that but means the
-- view now runs as its bypassrls owner, so estimation_runs' RLS never binds inside
-- it: the aggregate spans every account.
--
-- LATENT TODAY, not a live leak: all 98 current runs sit on the shared SYSTEM account
-- (00000000-0000-0000-0000-000000000000), which is also estimation_runs.account_id's
-- DEFAULT, and the RLS policy's own zero-UUID arm makes those rows visible to every
-- authenticated caller by design. The leak activates when the estimation write path
-- starts stamping real per-account ids AND a second tenant exists. The exposed surface
-- is estimation METADATA (which properties have estimates, how many, when) -- never
-- estimate values or inputs.
--
-- The view must STAY owner-rights: flipping security_invoker back on reintroduces the
-- 316 deny-all (verified again here -- `listings` still has RLS enabled with zero
-- policies). Instead the read policy is mirrored as an in-body predicate, the same
-- technique migration 318 uses for is_platform_admin() on 26 objects.
--
-- The predicate reproduces ALL THREE arms of estimation_runs_tenant_read verbatim.
-- This matters: the obvious two-arm form (own accounts OR platform admin) was probed
-- live under `set local role authenticated` and returns ZERO rows, because every
-- current run lives on the shared SYSTEM account -- i.e. the naive fix would have
-- re-created the exact migration-316 regression this batch started from. The
-- unconditional SYSTEM-account arm is what preserves today's market-wide behaviour.
--
-- Semantics after this migration: runs on the SYSTEM account stay visible to everyone
-- (unchanged, and consistent with the RLS policy); a run stamped with a real account
-- is visible only to that account's members, plus platform admins for NULL-account
-- rows. browse_stats_properties (SECURITY INVOKER, EXISTS over this view) inherits
-- the same scoping automatically -- no change needed there.
--
-- CREATE OR REPLACE VIEW keeps the column list, the reloptions (security_invoker=false
-- from 329), the `authenticated` SELECT grant (319) and the anon revoke (331), so no
-- grant statements are re-issued here.

begin;

set local lock_timeout = '5s';

create or replace view public.property_estimates_public as
with matched as (
  select l.property_id,
         er.created_at
    from estimation_runs er
    join listings l on l.sreality_id = er.input_sreality_id
   where er.status = 'success'::text
     and er.input_sreality_id is not null
     and l.property_id is not null
     and ( er.account_id in (select current_account_ids())
        or er.account_id = '00000000-0000-0000-0000-000000000000'::uuid
        or (er.account_id is null and is_platform_admin()) )
  union all
  select l.property_id,
         er.created_at
    from estimation_runs er
    join listings l on l.source_url = er.input_url
   where er.status = 'success'::text
     and er.input_sreality_id is null
     and l.property_id is not null
     and ( er.account_id in (select current_account_ids())
        or er.account_id = '00000000-0000-0000-0000-000000000000'::uuid
        or (er.account_id is null and is_platform_admin()) )
)
select property_id,
       count(*)::integer as run_count,
       max(created_at) as last_run_at
  from matched
 group by property_id;

-- Post-conditions: structural + data-independent. A row-count assertion would be
-- vacuous on the empty CI schema replay and misleading here (this block runs as a
-- bypassrls superuser whose claims-less session matches the SYSTEM-account arm).
do $$
declare
  v_invoker text;
  v_def text;
begin
  select coalesce(
           (select option_value from pg_options_to_table(c.reloptions)
             where option_name = 'security_invoker'),
           'false')
    into v_invoker
    from pg_class c
   where c.oid = 'public.property_estimates_public'::regclass;
  if v_invoker <> 'false' then
    raise exception 'property_estimates_public must stay owner-rights (security_invoker=%)', v_invoker;
  end if;

  v_def := pg_get_viewdef('public.property_estimates_public'::regclass, true);
  if position('current_account_ids' in v_def) = 0 then
    raise exception 'property_estimates_public lost its per-account scoping predicate';
  end if;
  if position('00000000-0000-0000-0000-000000000000' in v_def) = 0 then
    raise exception 'property_estimates_public lost the shared SYSTEM-account arm — '
                    'Browse''s "with estimates" filter would return nothing';
  end if;

  perform 1 from public.property_estimates_public limit 1;
end $$;

commit;
