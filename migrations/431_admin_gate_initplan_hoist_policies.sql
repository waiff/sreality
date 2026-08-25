-- 431_admin_gate_initplan_hoist_policies.sql
-- Cardinality Doctrine W1a — hoist is_platform_admin() into an InitPlan, in the 10 RLS
-- policies where it is actually evaluated per row.
--
-- THE DEFECT. is_platform_admin() is STABLE and argument-less, so ONE evaluation per
-- statement is exactly what STABLE promises — and Postgres would do that itself if the
-- OR structure did not hide the pseudoconstancy. In all 10 policies the gate sits inside
-- an OR with column references, so it is not pseudoconstant and the executor calls a
-- SECURITY DEFINER function once per candidate row. Measured live:
--
--   before:  Filter: (... OR ((account_id = '000...0') AND is_platform_admin()))
--   after:   InitPlan 2 -> Result
--            Filter: (... OR ((account_id = '000...0') AND (InitPlan 2).col1))
--
-- SEMANTICS ARE BIT-IDENTICAL. The function keeps reading live on every statement, so
-- revocation stays instantaneous: delete the admins row and the very next statement
-- denies. No cache, no TTL, no memo. This is the Supabase-documented RLS idiom, and the
-- repo already relies on it implicitly for current_account_ids().
--
-- ALTER POLICY, NOT DROP + CREATE. Three reasons, in increasing order of importance:
--   1. no transient window where a table is unpoliced;
--   2. policy OIDs stay stable, so a before/after comparison can join on identity;
--   3. DECISIVE: ALTER POLICY cannot lose `TO authenticated`, `AS PERMISSIVE` or the
--      command — they are untouched when omitted. A DROP + CREATE that forgets
--      `to authenticated` silently widens the policy to PUBLIC. That is a
--      privilege-escalation foot-gun, and this is a security replay.
--
-- The four *_tenant_rw ALL policies carry the gate ONLY in their qual. Their with_check
-- is the shorter two-arm predicate with NO admin arm, so `USING` is specified alone and
-- with_check is deliberately left untouched — a scripted "wrap both expressions" pass
-- would hallucinate a gate into it, which is a privilege change, not a perf change.
--
-- procost 100 and proparallel 'u' on is_platform_admin() are DELIBERATELY unchanged.
-- Both are planner inputs, and a plan-affecting edit does not belong in a security replay
-- whose whole discipline is verbatim-with-wrapper.
--
-- Scope: 11 call sites across 10 policies, 9 tables. The 36 views/functions that also
-- reference the gate are a separate migration — they carry no per-row evaluation, and
-- CREATE OR REPLACE on them has its own hazards (it resets view reloptions and drops
-- unrestated function attributes) that do not belong in the same lock window as this.
--
-- Rollback: migrations/431_revert_admin_gate_initplan_hoist_policies.sql (shipped
-- unapplied in this PR).

begin;

-- Never head-block production: 9 of these tables are written continuously by the API and
-- the realtime worker. A timeout rolls the whole thing back, which is safe — retry.
set local lock_timeout = '3s';
set local statement_timeout = '120s';

-- ---------------------------------------------------------------------------
-- Shape A x6 — gate guarded by `account_id IS NULL`, USING only.
-- ---------------------------------------------------------------------------
alter policy building_run_attachments_tenant_rw on public.building_run_attachments
  using (
    (account_id in (select current_account_ids()))
    or account_id = '00000000-0000-0000-0000-000000000000'::uuid
    or account_id is null and (select is_platform_admin())
  );

alter policy building_runs_tenant_read on public.building_runs
  using (
    (account_id in (select current_account_ids()))
    or account_id = '00000000-0000-0000-0000-000000000000'::uuid
    or account_id is null and (select is_platform_admin())
  );

alter policy estimation_cohort_entries_tenant_rw on public.estimation_cohort_entries
  using (
    (account_id in (select current_account_ids()))
    or account_id = '00000000-0000-0000-0000-000000000000'::uuid
    or account_id is null and (select is_platform_admin())
  );

alter policy estimation_feedback_tenant_rw on public.estimation_feedback
  using (
    (account_id in (select current_account_ids()))
    or account_id = '00000000-0000-0000-0000-000000000000'::uuid
    or account_id is null and (select is_platform_admin())
  );

alter policy estimation_runs_tenant_read on public.estimation_runs
  using (
    (account_id in (select current_account_ids()))
    or account_id = '00000000-0000-0000-0000-000000000000'::uuid
    or account_id is null and (select is_platform_admin())
  );

alter policy estimation_trace_payloads_tenant_rw on public.estimation_trace_payloads
  using (
    (account_id in (select current_account_ids()))
    or account_id = '00000000-0000-0000-0000-000000000000'::uuid
    or account_id is null and (select is_platform_admin())
  );

-- ---------------------------------------------------------------------------
-- Shape B x1 — notification_dispatches ANDs the gate with the PLATFORM account id,
-- not with `account_id IS NULL` (migration 364 superseded 292's version). A find/replace
-- keyed on shape A's literal text silently skips this one.
-- ---------------------------------------------------------------------------
alter policy notification_dispatches_tenant_read on public.notification_dispatches
  using (
    (account_id in (select current_account_ids()))
    or account_id = '00000000-0000-0000-0000-000000000000'::uuid and (select is_platform_admin())
  );

-- ---------------------------------------------------------------------------
-- Shape C x1 — llm_calls reaches the gate through a correlated EXISTS over
-- estimation_runs. The wrap is safe (the function takes no arguments and reads only
-- session/JWT state, so it depends on neither the outer llm_calls row nor the inner er
-- row) but the win is BOUNDED and worth stating: the uncorrelated sublink's ParamExec
-- slot is filled once for the whole query, while the correlated EXISTS still runs per
-- llm_calls row.
-- ---------------------------------------------------------------------------
alter policy llm_calls_tenant_read on public.llm_calls
  using (
    exists (
      select 1
        from estimation_runs er
       where er.id = llm_calls.estimation_run_id
         and ((er.account_id in (select current_account_ids()))
              or er.account_id = '00000000-0000-0000-0000-000000000000'::uuid
              or er.account_id is null and (select is_platform_admin()))
    )
  );

-- ---------------------------------------------------------------------------
-- Standalone gate x3 sites on 2 policies. BOTH paren pairs are required: the outer pair
-- is expression syntax, the inner pair is the subselect. `with check (select f())` is a
-- syntax error — a bare SELECT is not an expression.
-- ---------------------------------------------------------------------------
alter policy manual_rental_estimates_admin_insert on public.manual_rental_estimates
  with check ((select is_platform_admin()));

alter policy manual_rental_estimates_admin_update on public.manual_rental_estimates
  using ((select is_platform_admin()))
  with check ((select is_platform_admin()));

-- ---------------------------------------------------------------------------
-- In-migration rail. Runs INSIDE the transaction, so a failure costs nothing: the
-- database is untouched and the error text is the diagnosis. This beats comparing against
-- a definition captured earlier from prod, because it compares before/after in the SAME
-- database and is therefore immune to prod-vs-replay drift.
--
-- Asserts three things:
--   (a) COMPLETENESS  — no bare is_platform_admin() call survives on any policy;
--   (b) COVERAGE      — exactly 11 wrapped sites across 10 policies remain;
--   (c) NO ATTRIBUTE DRIFT — command, permissive flag and role list are unchanged, and
--       the four *_tenant_rw with_check expressions did NOT gain an admin arm.
-- ---------------------------------------------------------------------------
do $$
declare
  bare_sites  int;
  wrapped     int;
  polcount    int;
  drifted     text;
begin
  select count(*) into bare_sites
    from pg_policy pol
    join pg_class c on c.oid = pol.polrelid
    join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'public'
     and (regexp_replace(coalesce(pg_get_expr(pol.polqual, pol.polrelid, true), ''),
            '\(\s*SELECT\s+is_platform_admin\(\)(\s+AS\s+is_platform_admin)?\s*\)', '', 'gi')
          like '%is_platform_admin%'
       or regexp_replace(coalesce(pg_get_expr(pol.polwithcheck, pol.polrelid, true), ''),
            '\(\s*SELECT\s+is_platform_admin\(\)(\s+AS\s+is_platform_admin)?\s*\)', '', 'gi')
          like '%is_platform_admin%');

  if bare_sites <> 0 then
    raise exception 'W1a INCOMPLETE: % policy expression(s) still call is_platform_admin() per row', bare_sites;
  end if;

  -- Count the WRAPPER, not the name. The deparser renders the wrapped form as
  -- `( SELECT is_platform_admin() AS is_platform_admin)` — the name appears TWICE per
  -- site, once as the call and once as the alias — so counting the name reports 22 for
  -- 11 sites. (This rail caught exactly that on its first run, and rolled back.)
  select count(*),
         sum((length(lower(txt)) - length(replace(lower(txt), 'select is_platform_admin()', ''))) / 26)
    into polcount, wrapped
    from (
      select coalesce(pg_get_expr(pol.polqual, pol.polrelid, true), '')
             || ' ' || coalesce(pg_get_expr(pol.polwithcheck, pol.polrelid, true), '') as txt
        from pg_policy pol
        join pg_class c on c.oid = pol.polrelid
        join pg_namespace n on n.oid = c.relnamespace
       where n.nspname = 'public'
         and (pg_get_expr(pol.polqual, pol.polrelid, true) like '%is_platform_admin%'
           or pg_get_expr(pol.polwithcheck, pol.polrelid, true) like '%is_platform_admin%')
    ) s;

  if (polcount, wrapped) is distinct from (10, 11) then
    raise exception 'W1a COVERAGE drift: % policies / % sites, expected 10 / 11', polcount, wrapped;
  end if;

  select string_agg(c.relname || '.' || pol.polname, ', ') into drifted
    from pg_policy pol
    join pg_class c on c.oid = pol.polrelid
    join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'public'
     and pol.polname in (
       'building_run_attachments_tenant_rw','building_runs_tenant_read',
       'estimation_cohort_entries_tenant_rw','estimation_feedback_tenant_rw',
       'estimation_runs_tenant_read','estimation_trace_payloads_tenant_rw',
       'llm_calls_tenant_read','notification_dispatches_tenant_read',
       'manual_rental_estimates_admin_insert','manual_rental_estimates_admin_update')
     and (not pol.polpermissive
          or pol.polroles::regrole[]::text[] is distinct from array['authenticated']);

  if drifted is not null then
    raise exception 'W1a ATTRIBUTE DRIFT on: % (permissive flag or role list moved)', drifted;
  end if;

  -- The four ALL policies must NOT have gained an admin arm in with_check.
  select string_agg(c.relname || '.' || pol.polname, ', ') into drifted
    from pg_policy pol
    join pg_class c on c.oid = pol.polrelid
    join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'public'
     and pol.polname in ('building_run_attachments_tenant_rw','estimation_cohort_entries_tenant_rw',
                         'estimation_feedback_tenant_rw','estimation_trace_payloads_tenant_rw')
     and pg_get_expr(pol.polwithcheck, pol.polrelid, true) like '%is_platform_admin%';

  if drifted is not null then
    raise exception 'W1a PRIVILEGE CHANGE: with_check gained an admin arm on: %', drifted;
  end if;
end $$;

commit;
