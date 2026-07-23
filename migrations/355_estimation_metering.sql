-- 355_estimation_metering.sql
-- Wave 1 metering substrate + the atomic submit-time gate spine (Phase 1 items
-- J + A9; docs/design/waves-1-4-public-features.md § Wave 1). Meters the paid
-- agent estimation: per SUCCESSFUL run against a MONTHLY quota (operator
-- decision 2026-07-22 — run-count, not USD), free plan = 3/mo, trial = 10.
--
-- Three pieces, all additive:
--   1. usage_ledger — one append-only row per metered action at terminal, cost
--      recorded for margin (the run's llm_calls sum). Per-account, RLS-scoped
--      like entitlements (mig 298): tenant reads its own, service-role writes.
--   2. estimation_runs.idempotency_key + a UNIQUE partial index over in-flight
--      rows — idempotency + per-(account,target) single-in-flight in ONE atomic
--      write (A9: a UNIQUE index + ON CONFLICT, never check-then-act which is
--      TOCTOU-racy over the tx pooler — the mig-279 lesson). NULL key (admin /
--      ClickUp / ungated callers) is excluded, so only metered submits dedupe.
--   3. plans.agent_estimations_monthly_quota — the per-plan run-count quota as
--      tunable DATA (free = 3). The trial (10) rides entitlements.status =
--      'trialing' via a plans.trial_* companion, resolved in the API.

begin;

create table if not exists usage_ledger (
  id                 bigserial primary key,
  account_id         uuid not null references accounts(id),
  action             text not null,          -- e.g. 'agent_estimation'
  cost_usd           numeric,                -- recorded for margin; NULL until known
  estimation_run_id  bigint references estimation_runs(id),
  created_at         timestamptz not null default now()
);

-- The rolling-window aggregate the budget gate reads: count/sum per account +
-- action within the current calendar month.
create index if not exists usage_ledger_account_action_created
  on usage_ledger (account_id, action, created_at desc);

alter table usage_ledger enable row level security;

-- Tenant reads its own usage; writes stay service-role (the run terminal path),
-- so no INSERT/UPDATE policy — same shape as entitlements (mig 298).
grant select on usage_ledger to authenticated;

create policy usage_ledger_tenant_read on usage_ledger
  for select to authenticated
  using (account_id in (select current_account_ids()));

-- Idempotency / single-in-flight key for metered submits. NULL for ungated
-- callers (admin/ClickUp/legacy) so the unique index below never constrains them.
alter table estimation_runs
  add column if not exists idempotency_key text;

-- At most ONE in-flight (pending|running) run per (account, idempotency_key):
-- the ON CONFLICT target the submit path uses to dedupe a double-submit into the
-- existing run atomically, with zero read-then-write race.
create unique index if not exists estimation_runs_inflight_idem
  on estimation_runs (account_id, idempotency_key)
  where status in ('pending', 'running') and idempotency_key is not null;

-- Per-plan monthly run-count quota for the paid agent estimation (tunable data;
-- an absent/NULL value reads as 0 = not entitled). Free = 3; a companion trial
-- allowance (10) applies while the account's entitlement is status='trialing'.
alter table plans
  add column if not exists agent_estimations_monthly_quota integer not null default 0,
  add column if not exists trial_agent_estimations_monthly_quota integer not null default 0;

update plans set agent_estimations_monthly_quota = 3,
                 trial_agent_estimations_monthly_quota = 10
 where key = 'free';

commit;
