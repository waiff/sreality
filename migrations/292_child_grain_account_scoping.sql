-- 292_child_grain_account_scoping.sql
-- Phase 1 increment 3, part 3/6 — account_id on child-grain tables, derived
-- from the owning parent by BEFORE trigger (a DEFAULT can't see NEW's sibling
-- columns; a trigger also covers ad-hoc SQL and forgotten code paths).
--
-- Policy shape: WITH CHECK re-verifies the trigger-derived account_id against
-- the caller's memberships. Postgres evaluates WITH CHECK on the row AFTER
-- BEFORE triggers run, so a tenant inserting a child that points at another
-- tenant's parent gets account_id NULL from the RLS-filtered parent lookup
-- (the trigger runs as the invoking role) and the insert FAILS CLOSED —
-- with check (true) here would instead write invisible orphan rows.
-- Service-role writers bypass RLS but still get correct stamping from the
-- trigger (their parent lookups see all rows).

begin;

-- ── collection_properties (parent: collections) ─────────────────────────
alter table collection_properties add column if not exists account_id uuid references accounts(id) on delete cascade;

create or replace function sync_collection_properties_account_id()
returns trigger language plpgsql as $$
begin
  select account_id into new.account_id from collections where id = new.collection_id;
  return new;
end;
$$;
drop trigger if exists collection_properties_account_id_biu on collection_properties;
create trigger collection_properties_account_id_biu
  before insert or update of collection_id on collection_properties
  for each row execute function sync_collection_properties_account_id();

revoke all on collection_properties from anon, authenticated;
grant select, insert, delete on collection_properties to authenticated;  -- membership rows are add/remove only
create policy collection_properties_tenant_rw on collection_properties
  for all to authenticated
  using (account_id in (select current_account_ids()))
  with check (account_id in (select current_account_ids()));

-- ── property_tags (parent: tags) ─────────────────────────────────────────
alter table property_tags add column if not exists account_id uuid references accounts(id) on delete cascade;

create or replace function sync_property_tags_account_id()
returns trigger language plpgsql as $$
begin
  select account_id into new.account_id from tags where id = new.tag_id;
  return new;
end;
$$;
drop trigger if exists property_tags_account_id_biu on property_tags;
create trigger property_tags_account_id_biu
  before insert or update of tag_id on property_tags
  for each row execute function sync_property_tags_account_id();

revoke all on property_tags from anon, authenticated;
grant select, insert, delete on property_tags to authenticated;
create policy property_tags_tenant_rw on property_tags
  for all to authenticated
  using (account_id in (select current_account_ids()))
  with check (account_id in (select current_account_ids()));

-- ── notification_dispatches (parent: subscription XOR collection) ─────────
-- Writes stay producer/service-role only (rule #16); tenants read their own
-- rows + update seen_at. source_kind='system_health' rows have no owning
-- tenant → NULL → admin-only visibility.
alter table notification_dispatches add column if not exists account_id uuid references accounts(id) on delete cascade;

create or replace function sync_notification_dispatches_account_id()
returns trigger language plpgsql as $$
begin
  if new.source_kind = 'watchdog' then
    select account_id into new.account_id from notification_subscriptions where id = new.subscription_id;
  elsif new.source_kind = 'collection_monitor' then
    select account_id into new.account_id from collections where id = new.collection_id;
  else
    new.account_id := null;
  end if;
  return new;
end;
$$;
drop trigger if exists notification_dispatches_account_id_biu on notification_dispatches;
create trigger notification_dispatches_account_id_biu
  before insert or update of subscription_id, collection_id, source_kind on notification_dispatches
  for each row execute function sync_notification_dispatches_account_id();

revoke all on notification_dispatches from anon, authenticated;
grant select, update on notification_dispatches to authenticated;
create policy notification_dispatches_tenant_read on notification_dispatches
  for select to authenticated
  using (account_id in (select current_account_ids())
         or (account_id is null and is_platform_admin()));
create policy notification_dispatches_tenant_update on notification_dispatches
  for update to authenticated
  using (account_id in (select current_account_ids()))
  with check (account_id in (select current_account_ids()));

-- ── estimation_cohort_entries (parent: estimation_runs) ──────────────────
alter table estimation_cohort_entries add column if not exists account_id uuid references accounts(id) on delete cascade;

create or replace function sync_estimation_cohort_entries_account_id()
returns trigger language plpgsql as $$
begin
  select account_id into new.account_id from estimation_runs where id = new.estimation_run_id;
  return new;
end;
$$;
drop trigger if exists estimation_cohort_entries_account_id_biu on estimation_cohort_entries;
create trigger estimation_cohort_entries_account_id_biu
  before insert or update of estimation_run_id on estimation_cohort_entries
  for each row execute function sync_estimation_cohort_entries_account_id();

revoke all on estimation_cohort_entries from anon, authenticated;
grant select, insert, update on estimation_cohort_entries to authenticated;
create policy estimation_cohort_entries_tenant_rw on estimation_cohort_entries
  for all to authenticated
  using (account_id in (select current_account_ids())
         or account_id = '00000000-0000-0000-0000-000000000000'
         or (account_id is null and is_platform_admin()))
  with check (account_id in (select current_account_ids())
              or account_id = '00000000-0000-0000-0000-000000000000');

-- ── estimation_trace_payloads (parent: estimation_runs) ──────────────────
alter table estimation_trace_payloads add column if not exists account_id uuid references accounts(id) on delete cascade;

create or replace function sync_estimation_trace_payloads_account_id()
returns trigger language plpgsql as $$
begin
  select account_id into new.account_id from estimation_runs where id = new.estimation_run_id;
  return new;
end;
$$;
drop trigger if exists estimation_trace_payloads_account_id_biu on estimation_trace_payloads;
create trigger estimation_trace_payloads_account_id_biu
  before insert or update of estimation_run_id on estimation_trace_payloads
  for each row execute function sync_estimation_trace_payloads_account_id();

revoke all on estimation_trace_payloads from anon, authenticated;
grant select, insert on estimation_trace_payloads to authenticated;  -- append-only trace
create policy estimation_trace_payloads_tenant_rw on estimation_trace_payloads
  for all to authenticated
  using (account_id in (select current_account_ids())
         or account_id = '00000000-0000-0000-0000-000000000000'
         or (account_id is null and is_platform_admin()))
  with check (account_id in (select current_account_ids())
              or account_id = '00000000-0000-0000-0000-000000000000');

-- ── estimation_feedback (parent: estimation_runs) ─────────────────────────
alter table estimation_feedback add column if not exists account_id uuid references accounts(id) on delete cascade;

create or replace function sync_estimation_feedback_account_id()
returns trigger language plpgsql as $$
begin
  select account_id into new.account_id from estimation_runs where id = new.estimation_run_id;
  return new;
end;
$$;
drop trigger if exists estimation_feedback_account_id_biu on estimation_feedback;
create trigger estimation_feedback_account_id_biu
  before insert or update of estimation_run_id on estimation_feedback
  for each row execute function sync_estimation_feedback_account_id();

revoke all on estimation_feedback from anon, authenticated;
grant select, insert, update on estimation_feedback to authenticated;
create policy estimation_feedback_tenant_rw on estimation_feedback
  for all to authenticated
  using (account_id in (select current_account_ids())
         or account_id = '00000000-0000-0000-0000-000000000000'
         or (account_id is null and is_platform_admin()))
  with check (account_id in (select current_account_ids())
              or account_id = '00000000-0000-0000-0000-000000000000');

-- ── building_run_attachments (parent: building_runs) ──────────────────────
alter table building_run_attachments add column if not exists account_id uuid references accounts(id) on delete cascade;

create or replace function sync_building_run_attachments_account_id()
returns trigger language plpgsql as $$
begin
  select account_id into new.account_id from building_runs where id = new.building_run_id;
  return new;
end;
$$;
drop trigger if exists building_run_attachments_account_id_biu on building_run_attachments;
create trigger building_run_attachments_account_id_biu
  before insert or update of building_run_id on building_run_attachments
  for each row execute function sync_building_run_attachments_account_id();

revoke all on building_run_attachments from anon, authenticated;
grant select, insert on building_run_attachments to authenticated;
create policy building_run_attachments_tenant_rw on building_run_attachments
  for all to authenticated
  using (account_id in (select current_account_ids())
         or account_id = '00000000-0000-0000-0000-000000000000'
         or (account_id is null and is_platform_admin()))
  with check (account_id in (select current_account_ids())
              or account_id = '00000000-0000-0000-0000-000000000000');

-- ── one-time sync for existing child rows whose parent already has account_id
-- (estimation_runs/building_runs were swept to the system account in 291;
--  collections/tags stay NULL until the operator backfill in 293, and these
--  UPDATEs are re-run by backfill_legacy_account_id for that wave).
update collection_properties cp set account_id = c.account_id
  from collections c where c.id = cp.collection_id and cp.account_id is distinct from c.account_id;
update property_tags pt set account_id = t.account_id
  from tags t where t.id = pt.tag_id and pt.account_id is distinct from t.account_id;
update estimation_cohort_entries ece set account_id = er.account_id
  from estimation_runs er where er.id = ece.estimation_run_id and ece.account_id is distinct from er.account_id;
update estimation_trace_payloads etp set account_id = er.account_id
  from estimation_runs er where er.id = etp.estimation_run_id and etp.account_id is distinct from er.account_id;
update estimation_feedback ef set account_id = er.account_id
  from estimation_runs er where er.id = ef.estimation_run_id and ef.account_id is distinct from er.account_id;
update building_run_attachments bra set account_id = br.account_id
  from building_runs br where br.id = bra.building_run_id and bra.account_id is distinct from br.account_id;

commit;
