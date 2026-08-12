-- 395: close the last Amendment-A6 broker surface still readable by a browser role.
--
-- WHY: `firms_public` (migration 187) carries a live `authenticated` SELECT that no
-- migration file ever grants — Supabase's public-schema DEFAULT ACL stamped it at
-- CREATE time, the same invisible drift migrations 331 and 349 cleaned up on other
-- objects. Migration 299's PART F swept the seven sibling broker relations plus the
-- broker_leaderboard function into the dark, but firms_public was not on that list,
-- so today a logged-in session can still read the whole firm rollup straight off
-- PostgREST while every other broker surface is served only through the
-- identity-gated, PII-masking /brokers API (api/routes/brokers.py).
--
-- Permission-narrowing only: no schema change, no data touched, nothing dropped.
-- Verified before writing this that nothing depends on the grant — the only
-- references to firms_public anywhere in the repo are migration 187 itself and two
-- docs; no frontend, API, toolkit, scraper, script or workflow reads it.
--
-- tests/test_migration_rls_grants.py adds "firms_public" to _BROKER_A6_SURFACES in
-- the same change, so no future migration can silently re-grant it.

begin;

set local lock_timeout = '5s';

revoke all on public.firms_public from anon, authenticated;

do $$
begin
  assert not has_table_privilege('authenticated', 'public.firms_public', 'SELECT'),
         'authenticated can still read firms_public — the revoke did not take';
  assert not has_table_privilege('anon', 'public.firms_public', 'SELECT'),
         'anon can still read firms_public — the revoke did not take';
end $$;

commit;
