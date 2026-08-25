-- 433 — drop public.browse_stats(...), the last basis-blind per-m2 cluster.
--
-- DESTRUCTIVE. Applied only with operator sign-off. Everything below is the
-- evidence for that decision and the exact route back.
--
-- WHAT IT IS. Migration 083's listing-grain Browse stats RPC: one sql function,
-- 46 arguments, returns jsonb, 299 lines. Superseded by
-- browse_stats_properties (property grain, migration 378, moved onto
-- public.measure_price_per_m2 in migration 425). It holds ELEVEN unfloored,
-- basis-blind price/area division expressions -- the largest single cluster of
-- re-derived per-m2 arithmetic left in the schema, and the one site rule #23's
-- census still carries as KIND_DEBT.
--
-- WHY IT IS SAFE TO DROP.
--   * Zero callers in the repo: 48 grep hits across api/, toolkit/,
--     frontend/src/, scripts/, chrome-extension/src/ and tests/, of which 27
--     name browse_stats_PROPERTIES and 21 are prose or comments. None is
--     invocation-shaped.
--   * Zero references inside the database: no other function body (pg_proc.
--     prosrc) and no view or matview definition (pg_get_viewdef) mentions it,
--     and pg_depend shows no object depending on its oid.
--   * No longer reachable from the perimeter. Migration 428 revoked EXECUTE
--     from anon and authenticated; only service_role retains it, and nothing
--     server-side calls it. (Before 428 it WAS reachable: the SPA runs as
--     `authenticated` once a Supabase Auth JWT is in hand, so any logged-in
--     session could POST /rest/v1/rpc/browse_stats and be served exactly the
--     numbers rule #23 says the platform no longer produces.)
--
-- HOW TO RESTORE IT. migrations/083_browse_stats_price_per_m2.sql is a
-- byte-exact restore script -- not "should be", VERIFIED: the function body
-- between its $$ delimiters is md5 42640f07a847723694ad6705468269d9 over 14,441
-- characters, and production's pg_proc.prosrc for this function is the same
-- md5 over the same length. Postgres stores prosrc verbatim, so re-running 083
-- reproduces the definition exactly. Re-granting is a separate decision: 083's
-- own grants are the pre-428 state, and 428's reasoning would apply again.
--
-- WHY THE SPELLING IS NOT NEGOTIABLE. tests/test_measure_registry_census.py
-- retires a migration's `create` only when a LATER migration's text matches its
-- `_SQL_DROP` regex, whose kind alternation is exactly
-- (materialized view|view|function|table) and whose name group is
-- [A-Za-z_][\w.]* with no quote handling. So `drop routine`, a double-quoted
-- identifier, and a 428-style `do $$ ... execute format('drop function %s') ...
-- $$` dynamic loop ALL leave 083's create in the effective set -- the function
-- would be gone while the census stayed green on a registration for an object
-- that no longer exists, which is the precise failure the rail exists to
-- prevent. Hence a plain, statement-level drop with the signature written out.
--
-- 428 deliberately used a DO block for the opposite reason: transcribing 46
-- argument types by hand is how a REVOKE silently targets nothing. That risk is
-- real here too, so the signature below was verified against the catalog before
-- merge -- to_regprocedure() on this exact string resolves to oid 226845, the
-- live function -- and `if exists` makes a miss loud rather than silent, since
-- a mistyped signature raises instead of no-oping.
--
-- PAIRED, IN THE SAME PR: the KIND_DEBT entry for
-- migrations/083_browse_stats_price_per_m2.sql::function:browse_stats is deleted
-- from toolkit.measures.REGISTERED_SITES. The two edits are strictly coupled --
-- this drop alone makes the census fail on a registration with no scanned site,
-- and that deletion alone makes it fail on 11 unregistered division hits.

drop function if exists public.browse_stats(
  text[], text[], integer, integer, integer, integer, boolean, integer,
  integer, integer, integer, integer, integer, boolean, boolean, boolean,
  boolean, text, boolean, boolean, boolean, integer, text[], bigint[],
  text, text, double precision, double precision, double precision,
  double precision, text, double precision, double precision, double precision,
  double precision, integer, double precision, double precision, text[],
  text[], jsonb, integer, integer, jsonb, double precision, double precision
);

-- Verification (run after applying):
--   select count(*) from pg_proc p
--     join pg_namespace n on n.oid = p.pronamespace
--    where n.nspname = 'public' and p.proname = 'browse_stats';
--   -> expected: 0
--
--   select count(*) from pg_proc p
--     join pg_namespace n on n.oid = p.pronamespace
--    where n.nspname = 'public' and p.proname = 'browse_stats_properties';
--   -> expected: 1  (the superseding function is untouched)
