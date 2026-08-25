-- 431_revert_admin_gate_initplan_hoist_policies.sql
--
-- REVERT for 431_admin_gate_initplan_hoist_policies.sql. SHIPPED UNAPPLIED.
--
-- It lives in migrations/reverts/ and NOT in migrations/ for two concrete reasons, both
-- verified rather than assumed:
--   * the CI schema replay applies `ls migrations/*.sql | sort`, which is NOT recursive —
--     a revert sitting beside the forward migration would be applied straight after it
--     and would silently undo the change in every replayed environment;
--   * tests/test_migration_numbers.py globs migrations/*.sql and forbids a duplicate
--     number above 304, so a second 431_* there would fail CI outright.
--
-- It exists so the rollback is a file that has been written and reviewed, not a hope.
--
-- Restores the exact per-row spelling captured from the live catalog immediately before
-- 431 was applied (pg_get_expr, 2026-08-25). Every predicate below is byte-for-byte the
-- pre-431 expression with the (select ...) wrapper removed and nothing else changed.
--
-- Applying this re-introduces a SECURITY DEFINER call per candidate row on 9 tables,
-- including llm_calls at 293,551 rows. It is a performance rollback, not a security one:
-- the semantics either way are identical.

begin;

set local lock_timeout = '3s';
set local statement_timeout = '120s';

alter policy building_run_attachments_tenant_rw on public.building_run_attachments
  using ((account_id in (select current_account_ids()))
         or account_id = '00000000-0000-0000-0000-000000000000'::uuid
         or account_id is null and is_platform_admin());

alter policy building_runs_tenant_read on public.building_runs
  using ((account_id in (select current_account_ids()))
         or account_id = '00000000-0000-0000-0000-000000000000'::uuid
         or account_id is null and is_platform_admin());

alter policy estimation_cohort_entries_tenant_rw on public.estimation_cohort_entries
  using ((account_id in (select current_account_ids()))
         or account_id = '00000000-0000-0000-0000-000000000000'::uuid
         or account_id is null and is_platform_admin());

alter policy estimation_feedback_tenant_rw on public.estimation_feedback
  using ((account_id in (select current_account_ids()))
         or account_id = '00000000-0000-0000-0000-000000000000'::uuid
         or account_id is null and is_platform_admin());

alter policy estimation_runs_tenant_read on public.estimation_runs
  using ((account_id in (select current_account_ids()))
         or account_id = '00000000-0000-0000-0000-000000000000'::uuid
         or account_id is null and is_platform_admin());

alter policy estimation_trace_payloads_tenant_rw on public.estimation_trace_payloads
  using ((account_id in (select current_account_ids()))
         or account_id = '00000000-0000-0000-0000-000000000000'::uuid
         or account_id is null and is_platform_admin());

alter policy notification_dispatches_tenant_read on public.notification_dispatches
  using ((account_id in (select current_account_ids()))
         or account_id = '00000000-0000-0000-0000-000000000000'::uuid and is_platform_admin());

alter policy llm_calls_tenant_read on public.llm_calls
  using (exists (
    select 1 from estimation_runs er
     where er.id = llm_calls.estimation_run_id
       and ((er.account_id in (select current_account_ids()))
            or er.account_id = '00000000-0000-0000-0000-000000000000'::uuid
            or er.account_id is null and is_platform_admin())));

alter policy manual_rental_estimates_admin_insert on public.manual_rental_estimates
  with check (is_platform_admin());

alter policy manual_rental_estimates_admin_update on public.manual_rental_estimates
  using (is_platform_admin())
  with check (is_platform_admin());

commit;
