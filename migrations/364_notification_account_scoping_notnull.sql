-- 364_notification_account_scoping_notnull.sql
--
-- Wave 3 (watchdogs & notifications), tenancy axis. Phase 1 (migrations 290/292)
-- gave notification_subscriptions + notification_dispatches account_id + RLS but
-- left the column NULLABLE, and migration 292's dispatch trigger stamped
-- source_kind='system_health' rows NULL (they have no owning tenant). Live: the
-- only NULL-account rows anywhere are 38 system_health dispatches. This migration
-- routes those to the SYSTEM account (admin-only visibility, mirroring the
-- estimation_runs SYSTEM arm) and makes account_id NOT NULL on both tables so the
-- ownership invariant is enforced by the schema, not just convention.
--
-- Ships in lockstep with api/routes/notifications.py + api/notifications.create_subscription
-- (which now resolve + stamp account_id on every subscription insert). Verified live
-- before applying: 0 NULL-account subscriptions/collections; all 38 NULL dispatches
-- are system_health; SYSTEM account 0000…0000 exists.

begin;

set local lock_timeout = '5s';

-- 1. Route the 38 orphan system_health dispatches to the SYSTEM account.
update notification_dispatches
   set account_id = '00000000-0000-0000-0000-000000000000'
 where account_id is null;

-- 2. Trigger: the else-branch (system_health, and any future ownerless kind) now
--    stamps SYSTEM instead of NULL, so no new NULL-account row can ever land.
create or replace function sync_notification_dispatches_account_id()
returns trigger language plpgsql as $$
begin
  if new.source_kind = 'watchdog' then
    select account_id into new.account_id from notification_subscriptions where id = new.subscription_id;
  elsif new.source_kind = 'collection_monitor' then
    select account_id into new.account_id from collections where id = new.collection_id;
  else
    new.account_id := '00000000-0000-0000-0000-000000000000';
  end if;
  return new;
end;
$$;

-- 3. Read policy: system_health rows are now SYSTEM-account, still admin-only.
--    (Unlike the estimation SYSTEM arm, system_health is operational-alert data
--     for the operator, NOT market data every tenant should see — so the SYSTEM
--     arm stays is_platform_admin()-gated, preserving today's admin-only semantics.)
drop policy if exists notification_dispatches_tenant_read on notification_dispatches;
create policy notification_dispatches_tenant_read on notification_dispatches
  for select to authenticated
  using (account_id in (select current_account_ids())
         or (account_id = '00000000-0000-0000-0000-000000000000' and is_platform_admin()));

-- 4. Enforce the invariant now that no NULLs remain.
alter table notification_dispatches   alter column account_id set not null;
alter table notification_subscriptions alter column account_id set not null;

commit;
