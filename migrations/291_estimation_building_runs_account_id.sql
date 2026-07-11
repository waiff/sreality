-- 291_estimation_building_runs_account_id.sql
-- Phase 1 increment 3, part 2/6 — account_id on estimation_runs/building_runs.
-- NULLABLE by design (Amendment A4): agent/system runs execute as service-role
-- with no JWT, so account_id can't come from a claims DEFAULT — instead the
-- column DEFAULTs to the SYSTEM account so service-role inserts land non-NULL
-- automatically, and user runs are stamped synchronously at the kickoff INSERT
-- once the API cuts over. Read policy: own account OR system runs; a NULL
-- (only reachable via a deleted account, on delete set null) stays visible to
-- platform admins instead of vanishing for everyone.

begin;

alter table estimation_runs add column if not exists account_id uuid
  references accounts(id) on delete set null
  default '00000000-0000-0000-0000-000000000000';
alter table building_runs add column if not exists account_id uuid
  references accounts(id) on delete set null
  default '00000000-0000-0000-0000-000000000000';

-- DEFAULT only governs future inserts — existing rows need the sweep.
update estimation_runs set account_id = '00000000-0000-0000-0000-000000000000' where account_id is null;
update building_runs   set account_id = '00000000-0000-0000-0000-000000000000' where account_id is null;

create index if not exists estimation_runs_account_id_idx on estimation_runs (account_id);
create index if not exists building_runs_account_id_idx   on building_runs   (account_id);

revoke all on estimation_runs from anon, authenticated;
revoke all on building_runs   from anon, authenticated;
grant select, insert, update on estimation_runs to authenticated;  -- no delete: rule #12 immutability
grant select, insert, update on building_runs   to authenticated;

create policy estimation_runs_tenant_read on estimation_runs
  for select to authenticated
  using (account_id in (select current_account_ids())
         or account_id = '00000000-0000-0000-0000-000000000000'
         or (account_id is null and is_platform_admin()));
create policy estimation_runs_tenant_insert on estimation_runs
  for insert to authenticated
  with check (account_id in (select current_account_ids()));
create policy estimation_runs_tenant_update on estimation_runs
  for update to authenticated
  using (account_id in (select current_account_ids()))
  with check (account_id in (select current_account_ids()));

create policy building_runs_tenant_read on building_runs
  for select to authenticated
  using (account_id in (select current_account_ids())
         or account_id = '00000000-0000-0000-0000-000000000000'
         or (account_id is null and is_platform_admin()));
create policy building_runs_tenant_insert on building_runs
  for insert to authenticated
  with check (account_id in (select current_account_ids()));
create policy building_runs_tenant_update on building_runs
  for update to authenticated
  using (account_id in (select current_account_ids()))
  with check (account_id in (select current_account_ids()));

commit;
