-- 357_pipeline_stage_invariant_and_board_index.sql
--
-- Wave 2 (opportunity pipeline management) hardening. Confirmed live against
-- `erlvtprrmrylhznfyaih` before writing this: the "genuinely new" pieces the
-- Wave 2 design called for (docs/design/waves-1-4-public-features.md) — the
-- cross-account stage-ownership composite FK, the per-account stage-key
-- uniques (Amendment A3), seed_default_pipeline(account_id) wired into
-- handle_new_user, and the account-partitioned merge reconciler (Amendment
-- A2) — all already shipped in migrations 294/295 (Phase 1 increment 3, PR
-- #763), ahead of this wave being picked up. The `/pipeline/*` +
-- `POST /listings/lookup` connection-swap onto the tenant pool also already
-- shipped (294/295's Python cutover + Wave 1's #886/#888/#899). This
-- migration ships the two remaining items the design called out that were
-- NOT yet done: the DB-level entry/terminal invariant (until now API-only —
-- see tests/test_tenant_isolation_live.py's `seeded_tenant_rows` comment) and
-- the account-leading board index.
--
-- The CHECK is pure defense-in-depth: api/pipeline.py's create_stage never
-- sets is_entry, and update_stage already 422s a combined entry+terminal
-- request at the app layer. Nothing live can violate it, so this is additive
-- and safe to apply without a data audit. `property_pipeline` is 14 rows
-- live — no CONCURRENTLY dance needed for the index.

begin;

alter table pipeline_stages
  add constraint pipeline_stages_entry_not_terminal check (not (is_entry and is_terminal));

create index if not exists property_pipeline_account_stage_board
  on property_pipeline (account_id, stage_id, board_position);

commit;
