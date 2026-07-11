-- 290_curation_account_scoping.sql
-- Phase 1 increment 3, part 1/6 — account_id + RLS for the freestanding
-- curation tables (docs/design/phase-1-multitenancy-foundations.md §4).
-- Additive and inert today: account_id stays NULL until the operator's first
-- signup runs backfill_legacy_account_id (migration 293); the service-role
-- API bypasses RLS structurally, and the anon SPA reads via the (still
-- definer) *_public views, so no current behavior changes.
--
-- manual_rental_estimates deliberately diverges from the tenant pattern: it is
-- shared market reference data (feeds gross-yield calcs platform-wide, like
-- curated_cities per rule #17), so account_id records provenance, reads are
-- platform-wide, and writes stay admin-only until the operator decides tenants
-- may contribute comps.

begin;

-- ── collections ──────────────────────────────────────────────────────────
alter table collections add column if not exists account_id uuid references accounts(id) on delete cascade;
create index if not exists collections_account_id_idx on collections (account_id);

-- collections_name_ci is a global functional unique index; re-scope per
-- account so every tenant can own e.g. its own protected 'monitoring'
-- collection (is_system rows are per-account too — the design doc's
-- "per-account system monitoring collection seed", not shared platform rows).
drop index if exists collections_name_ci;
create unique index collections_name_ci on collections (account_id, lower(name));

revoke all on collections from anon, authenticated;
grant select, insert, update, delete on collections to authenticated;

create policy collections_tenant_rw on collections
  for all to authenticated
  using (account_id in (select current_account_ids()))
  with check (account_id in (select current_account_ids()));

-- ── tags ─────────────────────────────────────────────────────────────────
alter table tags add column if not exists account_id uuid references accounts(id) on delete cascade;
create index if not exists tags_account_id_idx on tags (account_id);

drop index if exists tags_name_ci;
create unique index tags_name_ci on tags (account_id, lower(name));

revoke all on tags from anon, authenticated;
grant select, insert, update, delete on tags to authenticated;

create policy tags_tenant_rw on tags
  for all to authenticated
  using (account_id in (select current_account_ids()))
  with check (account_id in (select current_account_ids()));

-- ── property_notes ───────────────────────────────────────────────────────
alter table property_notes add column if not exists account_id uuid references accounts(id) on delete cascade;
create index if not exists property_notes_account_id_idx on property_notes (account_id);

revoke all on property_notes from anon, authenticated;
grant select, insert, update, delete on property_notes to authenticated;

create policy property_notes_tenant_rw on property_notes
  for all to authenticated
  using (account_id in (select current_account_ids()))
  with check (account_id in (select current_account_ids()));

-- ── filter_presets ────────────────────────────────────────────────────────
alter table filter_presets add column if not exists account_id uuid references accounts(id) on delete cascade;
create index if not exists filter_presets_account_id_idx on filter_presets (account_id);

revoke all on filter_presets from anon, authenticated;
grant select, insert, update, delete on filter_presets to authenticated;

create policy filter_presets_tenant_rw on filter_presets
  for all to authenticated
  using (account_id in (select current_account_ids()))
  with check (account_id in (select current_account_ids()));

-- ── notification_subscriptions ────────────────────────────────────────────
-- The watchdog matcher iterates active subscriptions cross-account as a
-- scheduled service-role job — it bypasses RLS structurally, unaffected.
alter table notification_subscriptions add column if not exists account_id uuid references accounts(id) on delete cascade;
create index if not exists notification_subscriptions_account_id_idx on notification_subscriptions (account_id);

revoke all on notification_subscriptions from anon, authenticated;
grant select, insert, update, delete on notification_subscriptions to authenticated;

create policy notification_subscriptions_tenant_rw on notification_subscriptions
  for all to authenticated
  using (account_id in (select current_account_ids()))
  with check (account_id in (select current_account_ids()));

-- ── manual_rental_estimates: shared platform reference, admin-only writes ──
alter table manual_rental_estimates add column if not exists account_id uuid references accounts(id) on delete set null;
create index if not exists manual_rental_estimates_account_id_idx on manual_rental_estimates (account_id);

revoke all on manual_rental_estimates from anon, authenticated;
grant select, insert, update on manual_rental_estimates to authenticated;

create policy manual_rental_estimates_read_all on manual_rental_estimates
  for select to authenticated
  using (true);
create policy manual_rental_estimates_admin_insert on manual_rental_estimates
  for insert to authenticated
  with check (is_platform_admin());
create policy manual_rental_estimates_admin_update on manual_rental_estimates
  for update to authenticated
  using (is_platform_admin())
  with check (is_platform_admin());

commit;
