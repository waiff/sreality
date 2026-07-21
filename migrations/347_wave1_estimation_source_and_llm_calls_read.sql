-- 347_wave1_estimation_source_and_llm_calls_read.sql
-- Wave 1 (W1-1) — schema delta for the extension-touched estimation routes'
-- move onto the tenant pool (docs/design/waves-1-4-public-features.md § Wave 1).
--
-- 1. estimation_runs.source gains 'extension' so POST /estimations can be
--    attributed correctly once the Chrome extension stops sending source='ui'.
-- 2. llm_calls carries RLS-enabled-with-zero-policies (like every internal
--    table swept by migration 299) — harmless while every reader is
--    service-role, but GET /estimations/{id} moving onto the tenant pool
--    (this PR) reads it via a scalar subselect for cost_usd_total, which
--    would silently return 0 for every real per-account caller. Scope the
--    read the same way estimation_trace_payloads/estimation_feedback
--    already are (migration 292): through the owning run's account.
--    llm_calls itself stays account_id-less by design (Wave 1 doc: "cost
--    auto-attributes via llm_calls.estimation_run_id — llm_calls needs no
--    account_id") — writes remain service-role only, no INSERT/UPDATE policy.
--    No migration ever explicitly GRANTed authenticated SELECT on llm_calls
--    (280's own comment: "aggregate — no grant on raw llm_calls") — the grant
--    live production has is Supabase's project-level default ACL from table
--    creation (migration 020), invisible to a from-scratch replay on plain
--    Postgres (CI caught this: a policy alone is not enough, GRANT is a
--    precondition RLS filters after). Explicit GRANT here makes the read
--    path fully reproducible from migrations alone; a no-op on production.

begin;

alter table estimation_runs drop constraint estimation_runs_source_check;
alter table estimation_runs add constraint estimation_runs_source_check
  check (source in ('ui', 'api', 'clickup', 'extension'));

grant select on llm_calls to authenticated;

create policy llm_calls_tenant_read on llm_calls
  for select to authenticated
  using (exists (
    select 1 from estimation_runs er
    where er.id = llm_calls.estimation_run_id
      and (er.account_id in (select current_account_ids())
           or er.account_id = '00000000-0000-0000-0000-000000000000'
           or (er.account_id is null and is_platform_admin()))
  ));

commit;
