-- 349_a5_properties_authenticated_read.sql
-- Phase 1 Amendment A5 (the column-safe half) — let the tenant pool read the
-- shared `properties` table directly.
--
-- Background. `properties` has been RLS-enabled-with-ZERO-policies since
-- migration 091, i.e. deny-all to every non-BYPASSRLS role. That was fine while
-- every request arrived on the static-token service-role bridge, but the Chrome
-- extension's own Supabase session (Wave 1) now drives real per-user JWTs
-- through `tenant_pool.tenant_conn`, which runs `SET LOCAL ROLE authenticated`.
-- Any tenant-conn handler that reads `properties` directly therefore gets zero
-- rows. Live-broken today for a signed-in user: adding a property note, adding
-- a property to a collection, and bookmarking into the deal pipeline — all three
-- call `toolkit.property_identity.resolve_active_property_ids`, whose recursive
-- survivor walk over `properties` returns nothing under deny-all, so the write
-- 404s or silently mis-resolves.
--
-- Why a plain `USING (true)` policy is SAFE here (unlike on `listings`). The
-- A5 correction (docs/design/phase-1-multitenancy-foundations.md) forbids a
-- blanket read policy on `listings` because that base table carries broker PII
-- inline (broker_email/phone/name, raw_json) and RLS filters rows, not columns,
-- so a permissive policy would expose those columns to every tenant via
-- PostgREST auto-REST. `properties` carries NO such columns — verified against
-- every `create/alter table properties` migration: its columns are market
-- rollups + geo + subtype/street only (no broker/contact/raw_json field ever
-- added). The broker contact visible on `properties_public` is joined in from
-- `listings` by that view, not stored on `properties`. So a tenant reading base
-- `properties` sees strictly LESS than the `properties_public` view they can
-- already read today. `listings` itself stays deny-all — the one tenant-conn
-- reader of it (create_note's id<->sreality_id map) is rerouted in the same PR
-- through the existing PII-free `listing_natural_key_public` identity view.
--
-- Scope. SELECT only; writes to `properties` stay service-role/BYPASSRLS
-- (grouping is out-of-band, rule #15). Service-role readers are unaffected
-- (BYPASSRLS ignores policies). The owner-bypass `properties_public` view is
-- unaffected (it never consulted base-table RLS). `admin_boundaries` and other
-- shared-market tables are intentionally NOT touched here — no tenant-conn
-- consumer reads them yet; add a policy when one does.

begin;

create policy properties_authenticated_read on properties
  for select to authenticated
  using (true);

commit;
