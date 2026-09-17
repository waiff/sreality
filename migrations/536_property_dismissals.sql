-- 536_property_dismissals.sql
-- Dismissal: "I reviewed this property and never want to see it again."
--
-- One durable, account-scoped fact per property. Discovery surfaces (Browse,
-- the notification feed + delivery) hide an actively dismissed property; the
-- operator can reveal and undo.
--
-- Shape follows migration 401 (broker_merge_suppressions), not a current-state
-- row: ACTIVE = lifted_at IS NULL, the partial unique index is both the
-- one-active-row-per-(property, account) rule and the anti-join probe, and undo
-- LIFTS the row instead of deleting it (rule #3) — so this table is its own
-- history and no separate event ledger exists. `lift_reason` separates the
-- operator's undo from the two automatic lifts ('pipeline': the property entered
-- the caller's deal pipeline, which always wins; 'merge': a duplicate active row
-- collapsed onto a merge survivor), so an undo rate is measurable.
--
-- Deliberately NOT in toolkit/operator_state.py's registry: that registry
-- collapses a colliding row by DELETING it, which would destroy history here.
-- toolkit/dismissal_identity.py carries these rows across a merge instead.
--
-- Tenancy follows migrations 290/294 (database skill, references/tenancy.md):
-- RLS on, one `for all` policy on current_account_ids(), `authenticated` DML so
-- the API's tenant_conn can write. UPDATE is column-scoped: a caller can lift a
-- dismissal, never rewrite one. No DELETE grant — nothing deletes a dismissal.
--
-- Additive. APPLY BEFORE MERGING: api/pipeline.py's add_card lifts dismissals,
-- so code that ships ahead of this table breaks every pipeline bookmark.

create table property_dismissals (
  id           bigserial   primary key,
  account_id   uuid        not null references accounts(id) on delete cascade,
  property_id  bigint      not null references properties(id) on delete cascade,
  dismissed_at timestamptz not null default now(),
  lifted_at    timestamptz,
  lift_reason  text        check (lift_reason in ('operator', 'pipeline', 'merge')),
  constraint property_dismissals_lift_pair
    check ((lifted_at is null) = (lift_reason is null))
);

create unique index property_dismissals_active_idx
  on property_dismissals (property_id, account_id) where lifted_at is null;

alter table property_dismissals enable row level security;

revoke all on property_dismissals from anon, authenticated;
grant select, insert on property_dismissals to authenticated;
grant update (lifted_at, lift_reason) on property_dismissals to authenticated;
grant usage on sequence property_dismissals_id_seq to authenticated;

create policy property_dismissals_tenant_rw on property_dismissals
  for all to authenticated
  using (account_id in (select current_account_ids()))
  with check (account_id in (select current_account_ids()));

-- The ONE read definition of "dismissed for the caller": active rows only,
-- scoped by the caller's RLS (security_invoker, migration 316's rule).
create view property_dismissals_public
  with (security_invoker = true) as
  select property_id, dismissed_at
  from property_dismissals
  where lifted_at is null;

revoke all on property_dismissals_public from anon, authenticated;
grant select on property_dismissals_public to authenticated;
