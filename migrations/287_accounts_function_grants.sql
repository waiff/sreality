-- 287_accounts_function_grants.sql
-- Follow-up to 286_accounts_foundation.sql: this Supabase project's public
-- schema has default privileges that explicitly GRANT EXECUTE on every new
-- function to anon/authenticated/service_role (the same default-ACL root
-- cause the Phase 0 audit found for tables — it materializes an explicit
-- ACL entry, not a bare PUBLIC grant, so `revoke ... from public` is a
-- no-op; confirmed via pg_proc.proacl). So the 3 new SECURITY DEFINER
-- functions were directly callable by anon via PostgREST RPC (Supabase
-- advisor: anon_security_definer_function_executable /
-- authenticated_security_definer_function_executable). Not a live data leak
-- (current_account_ids()/is_platform_admin() key off the JWT sub claim,
-- which anon never has, so both return empty under anon), but this
-- project's posture is: close every anon-reachable surface, don't rely on a
-- function's internal logic to make an unnecessary grant harmless.
--
-- current_account_ids() / is_platform_admin() ARE meant to run as
-- `authenticated` (RLS policies invoke them under the querying role), so they
-- keep an explicit authenticated grant. handle_new_user() is a trigger-only
-- function (references NEW/OLD) — the trigger manager invokes it directly
-- regardless of the inserting session's privileges, so it gets no grant at all.

begin;

revoke execute on function current_account_ids() from anon, authenticated;
revoke execute on function is_platform_admin() from anon, authenticated;
revoke execute on function handle_new_user() from anon, authenticated;

grant execute on function current_account_ids() to authenticated;
grant execute on function is_platform_admin() to authenticated;

commit;
