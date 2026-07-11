-- 294_pipeline_account_scoping.sql
-- Phase 1 increment 3, part 5/6 — pipeline account columns, per-account stage
-- uniques, RLS, the signup seeds, and the legacy backfill machinery.
--
-- DELIBERATELY NOT HERE (moved to 295, applied only with the Python cutover):
-- the property_pipeline PK swap. `ON CONFLICT (property_id)` in
-- api/pipeline.py add_card + reconcile_pipeline_on_unmerge cannot infer
-- against a composite unique index, so dropping the single-column PK now
-- would 42P10 every bookmark and every unmerge while the deployed code still
-- targets (property_id). The PK stays until 295 ships in the same deploy as
-- the rewritten code.
--
-- The stage uniques CAN re-key now (Amendment A3): the deployed add_card
-- looks up `where is_entry limit 1`, which keeps returning the operator's
-- one entry stage unchanged while account_id is NULL / single-account.

begin;

alter table property_pipeline        add column if not exists account_id uuid references accounts(id) on delete cascade;
alter table pipeline_stages          add column if not exists account_id uuid references accounts(id) on delete cascade;
alter table property_pipeline_events add column if not exists account_id uuid references accounts(id) on delete cascade;

-- Both global uniques re-keyed per account (A3) — exact live index names.
drop index if exists pipeline_stages_key_ci;
create unique index pipeline_stages_key_ci on pipeline_stages (account_id, lower(key));

drop index if exists pipeline_stages_one_entry;
create unique index pipeline_stages_one_entry on pipeline_stages (account_id, is_entry) where is_entry;

create index if not exists property_pipeline_account_id_idx on property_pipeline (account_id);
create index if not exists property_pipeline_events_account_id_idx on property_pipeline_events (account_id);

revoke all on property_pipeline, pipeline_stages, property_pipeline_events from anon, authenticated;
grant select, insert, update, delete on property_pipeline to authenticated;
grant select, insert, update, delete on pipeline_stages to authenticated;
grant select, insert on property_pipeline_events to authenticated;  -- append-only ledger

create policy property_pipeline_tenant_rw on property_pipeline
  for all to authenticated
  using (account_id in (select current_account_ids()))
  with check (account_id in (select current_account_ids()));

create policy pipeline_stages_tenant_rw on pipeline_stages
  for all to authenticated
  using (account_id in (select current_account_ids()))
  with check (account_id in (select current_account_ids()));

create policy property_pipeline_events_tenant_rw on property_pipeline_events
  for all to authenticated
  using (account_id in (select current_account_ids()))
  with check (account_id in (select current_account_ids()));

do $$
declare
  t text;
  seq text;
begin
  foreach t in array array['pipeline_stages','property_pipeline_events'] loop
    if exists (select 1 from information_schema.columns
               where table_schema = 'public' and table_name = t and column_name = 'id') then
      seq := pg_get_serial_sequence(t, 'id');
      if seq is not null then
        execute format('grant usage on sequence %s to authenticated', seq);
      end if;
    end if;
  end loop;
end $$;

-- ── seed_default_pipeline(account_id): the default board, per account ──────
-- Stage keys/labels mirror the operator's live board so a post-backfill
-- re-seed no-ops on the per-account unique.
create or replace function seed_default_pipeline(target_account_id uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into pipeline_stages (account_id, key, label, position, color, is_entry, is_terminal)
  values
    (target_account_id, 'interested', '1. For Review',   1, 'slate',  true,  false),
    (target_account_id, 'viewing',    '2. For Call',     2, 'ochre',  false, false),
    (target_account_id, 'offer',      '3. For Visit',    3, 'sage',   false, false),
    (target_account_id, 'won',        '4. Negotiations', 4, 'copper', false, false),
    (target_account_id, 'lost',       '9. Passed',       5, 'sand',   false, true),
    (target_account_id, '9_bought',   '9. Bought',       6, 'sand',   false, true),
    (target_account_id, '9_lost',     '9. Lost',         7, 'sand',   false, true)
  on conflict do nothing;
end;
$$;
revoke execute on function seed_default_pipeline(uuid) from anon, authenticated;

-- ── seed_default_collections(account_id): the protected monitoring collection
create or replace function seed_default_collections(target_account_id uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into collections (account_id, name, is_system)
  values (target_account_id, 'monitoring', true)
  on conflict do nothing;
end;
$$;
revoke execute on function seed_default_collections(uuid) from anon, authenticated;

-- ── backfill_legacy_account_id(account_id): sweeps ALL pre-tenancy NULLs ────
-- The operator's existing monitoring collection and pipeline board transfer
-- to them wholesale (is_system collections are per-account, not platform).
create or replace function backfill_legacy_account_id(target_account_id uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  update collections set account_id = target_account_id where account_id is null;
  update tags set account_id = target_account_id where account_id is null;
  update property_notes set account_id = target_account_id where account_id is null;
  update filter_presets set account_id = target_account_id where account_id is null;
  update notification_subscriptions set account_id = target_account_id where account_id is null;
  update manual_rental_estimates set account_id = '00000000-0000-0000-0000-000000000000' where account_id is null;

  update collection_properties cp set account_id = c.account_id
    from collections c where c.id = cp.collection_id and cp.account_id is null;
  update property_tags pt set account_id = t.account_id
    from tags t where t.id = pt.tag_id and pt.account_id is null;
  update notification_dispatches nd set account_id = ns.account_id
    from notification_subscriptions ns
    where ns.id = nd.subscription_id and nd.source_kind = 'watchdog' and nd.account_id is null;
  update notification_dispatches nd set account_id = c.account_id
    from collections c
    where c.id = nd.collection_id and nd.source_kind = 'collection_monitor' and nd.account_id is null;
  update estimation_cohort_entries ece set account_id = er.account_id
    from estimation_runs er where er.id = ece.estimation_run_id and ece.account_id is null;
  update estimation_trace_payloads etp set account_id = er.account_id
    from estimation_runs er where er.id = etp.estimation_run_id and etp.account_id is null;
  update estimation_feedback ef set account_id = er.account_id
    from estimation_runs er where er.id = ef.estimation_run_id and ef.account_id is null;
  update building_run_attachments bra set account_id = br.account_id
    from building_runs br where br.id = bra.building_run_id and bra.account_id is null;

  update property_pipeline set account_id = target_account_id where account_id is null;
  update pipeline_stages set account_id = target_account_id where account_id is null;
  update property_pipeline_events set account_id = target_account_id where account_id is null;
end;
$$;
revoke execute on function backfill_legacy_account_id(uuid) from anon, authenticated;

-- ── atomic first-signup claim (A9: never check-then-act) ───────────────────
-- The unique-key INSERT is the atomic test-and-set: exactly one signup ever
-- sees a non-null RETURNING, so exactly one backfill ever runs — the same
-- lease-row CAS pattern migration 279 adopted after session advisory locks
-- proved unsound over the transaction pooler.
create table if not exists legacy_backfill_claim (
  claim_key  text primary key,
  account_id uuid not null references accounts(id),
  claimed_at timestamptz not null default now()
);
revoke all on legacy_backfill_claim from anon, authenticated;

-- ── the signup trigger: claim FIRST, then backfill XOR seed ────────────────
-- The first personal signup (the operator, while Google OAuth is the only
-- gate) inherits ALL legacy state via backfill — seeding for them too would
-- collide with the backfilled board/collection on the new per-account
-- uniques. Every later signup gets fresh seeds instead. Revisit before any
-- public signup surface ships: "first signup wins the legacy data" is only
-- safe while the operator is guaranteed first.
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
  end if;

  return new;
end;
$$;

revoke execute on function handle_new_user() from anon, authenticated;

commit;
