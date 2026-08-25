-- 428 — close the last reachable per-m2 re-derivation (rule #23).
--
-- public.browse_stats(...) is migration 083's listing-grain Browse stats RPC.
-- It has been superseded by browse_stats_properties (migration 378, moved onto
-- public.measure_price_per_m2 in migration 425) and has ZERO callers: none in
-- api/, toolkit/, frontend/src/ or scripts/ (the SPA calls only
-- browse_stats_properties), and no other function or view in public references
-- it either.
--
-- But it was still EXECUTE-granted to `authenticated`, and the SPA runs as
-- `authenticated` once a Supabase Auth user JWT is in hand. So the function was
-- reachable as POST /rest/v1/rpc/browse_stats, returning eleven unfloored,
-- basis-blind per-square-metre aggregates -- exactly the numbers rule #23 says
-- the platform no longer produces. Registered as KIND_DEBT in
-- toolkit.measures.REGISTERED_SITES, which is honest about the definition but
-- must not be read as "inert".
--
-- ADDITIVE, and autonomous under the database gate: this revokes a privilege,
-- it does not drop the function. Nothing loses a capability it uses, and a
-- single GRANT restores it. The `drop function` itself is DESTRUCTIVE and still
-- waits on operator confirmation plus a pg_dump; until then the definition
-- stays in the catalog but is no longer reachable from the perimeter.
--
-- A DO block over pg_proc rather than a transcribed 46-argument signature:
-- transcribing that signature by hand is how a revoke silently targets nothing.

do $$
declare
  r record;
  n int := 0;
begin
  for r in
    select p.oid::regprocedure::text as sig
      from pg_proc p
      join pg_namespace ns on ns.oid = p.pronamespace
     where ns.nspname = 'public'
       and p.proname = 'browse_stats'
  loop
    execute format('revoke execute on function %s from authenticated, anon', r.sig);
    n := n + 1;
  end loop;

  if n = 0 then
    raise notice '428: no public.browse_stats overload found -- already dropped';
  else
    raise notice '428: revoked EXECUTE on % browse_stats overload(s)', n;
  end if;
end
$$;

-- Verification (run after applying):
--   select has_function_privilege('authenticated', p.oid, 'EXECUTE')
--     from pg_proc p join pg_namespace n on n.oid = p.pronamespace
--    where n.nspname = 'public' and p.proname = 'browse_stats';
--   -> expected: false
