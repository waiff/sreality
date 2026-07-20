-- 317_rls_dedup_model_compare_sets.sql  (Phase 0 follow-through, same pattern as 301)
-- Migration 304 (dedup_model_compare_review, a parallel session) created
-- `dedup_model_compare_sets` with no RLS or grants. Not anon/authenticated-reachable --
-- migration 299's default-ACL fix means the new table auto-received zero browser-role
-- grants (verified live: no anon/authenticated rows in information_schema.role_table_grants).
-- Still needs deny-all RLS for defense-in-depth, like every other internal table 299/301
-- closed, and to satisfy tests/test_migration_rls_grants.py::test_new_base_tables_enable_rls.
-- Service-role / postgres (BYPASSRLS) keep full access; nothing reads it via a browser role.

begin;
alter table public.dedup_model_compare_sets enable row level security;
revoke all on public.dedup_model_compare_sets from anon, authenticated;
commit;
