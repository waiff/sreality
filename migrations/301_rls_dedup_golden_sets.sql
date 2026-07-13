-- 301_rls_dedup_golden_sets.sql  (Phase 0 follow-through)
-- Migration 300 (dedup golden-set foundation, a parallel session) created
-- `dedup_golden_sets` with no RLS or grants. It is NOT anon-reachable — migration
-- 299's default-ACL fix meant the new table auto-received zero browser-role grants
-- (verified live: anon/authenticated have no SELECT/INSERT on it) — which is exactly
-- the root-cause fix proving itself. But it must still carry deny-all RLS for
-- defense-in-depth, like every other internal table 299 closed. Service-role /
-- postgres (BYPASSRLS) keep full access; nothing reads it via a browser role.
-- The dedup_label_events VIEW that 300 also created needs no RLS (views can't).

begin;
alter table public.dedup_golden_sets enable row level security;
revoke all on public.dedup_golden_sets from anon, authenticated;
commit;
