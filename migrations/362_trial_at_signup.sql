-- 362_trial_at_signup.sql
--
-- Wave 1: grant every NEW signup a 7-day trial (10 agent estimations). The
-- metering substrate (mig 355, api/estimation_runs.py:_resolve_entitlement)
-- already honors entitlements.status='trialing' + unexpired current_period_end
-- -> the plan's trial_agent_estimations_monthly_quota (free plan = 10), falling
-- back to agent_estimations_monthly_quota (3) once the window closes. Nothing
-- marked new signups trialing until now, so they silently got the 3/mo default.
--
-- This wires a SECURITY DEFINER seed_trial_entitlement() (mirroring
-- seed_default_pipeline / seed_default_collections) into handle_new_user's
-- fresh-signup branch ONLY. The legacy-backfill branch is the operator's first
-- signup: they are a platform admin and bypass metering entirely, so a trial
-- row there would be dead weight (and its plan='free' would misreport their
-- billing view). Trial length is hardcoded (7 days) the same way the seed
-- functions hardcode their rows.
--
-- Additive: a new function + a CREATE OR REPLACE of handle_new_user that only
-- adds one perform call to its existing body (verified byte-for-byte against the
-- live definition first). Applied live via the Supabase MCP before this file.

begin;

create or replace function seed_trial_entitlement(target_account_id uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  -- plan='free' + status='trialing' -> _resolve_entitlement returns the free
  -- plan's trial quota (10) while unexpired, then its 3/mo after the window.
  -- ON CONFLICT keeps this idempotent (a re-seed / double trigger no-ops).
  insert into entitlements (account_id, plan, status, current_period_end)
  values (target_account_id, 'free', 'trialing', now() + interval '7 days')
  on conflict (account_id) do nothing;
end;
$$;
revoke execute on function seed_trial_entitlement(uuid) from anon, authenticated;

-- CREATE OR REPLACE of handle_new_user: identical to the live definition, plus
-- one `perform seed_trial_entitlement` in the else (fresh-signup) branch.
create or replace function handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  new_account_id uuid;
  claimed_account_id uuid;
begin
  insert into accounts (kind, name)
    values ('personal', coalesce(new.email, new.id::text))
    returning id into new_account_id;
  insert into account_members (account_id, user_id, role)
    values (new_account_id, new.id, 'owner');

  insert into legacy_backfill_claim (claim_key, account_id)
    values ('legacy_backfill_v1', new_account_id)
    on conflict (claim_key) do nothing
    returning account_id into claimed_account_id;

  if claimed_account_id is not null then
    perform backfill_legacy_account_id(new_account_id);
  else
    perform seed_default_pipeline(new_account_id);
    perform seed_default_collections(new_account_id);
    perform seed_trial_entitlement(new_account_id);
  end if;

  return new;
end;
$$;
revoke execute on function handle_new_user() from anon, authenticated;

commit;
