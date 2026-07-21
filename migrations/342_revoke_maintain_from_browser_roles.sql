-- 342_revoke_maintain_from_browser_roles.sql
--
-- Makes migration 331's MAINTAIN revoke durable. Plan:
-- docs/design/public-release-remediation-round2.md § PR-C.
--
-- PostgreSQL 17 added the MAINTAIN privilege (REFRESH MATERIALIZED VIEW, VACUUM,
-- ANALYZE, CLUSTER, REINDEX, LOCK TABLE). This project's postgres DEFAULT ACL grants
-- `authenticated` SELECT+MAINTAIN (`rm`) on every new relation, so migration 331's
-- one-time revoke could not hold: `properties_map_mv` is DROP+CREATEd on every
-- blue-green rebuild (rebuild_properties_map_mv, migrations 277/299), which re-grants
-- MAINTAIN from the default ACL within ~30 minutes. Verified live: 331 stripped it on
-- 2026-07-20 and it was back on 2026-07-21, alongside 84 base tables that carry it.
--
-- Severity is posture, not incident: no browser path can reach it. PostgREST and the
-- API's tenant pool issue only DML/queries -- REFRESH/VACUUM/CLUSTER are utility
-- statements, and VACUUM cannot run inside the tenant pool's transaction at all. The
-- revoke is therefore behaviour-neutral; it removes a privilege nothing uses.
--
-- The DEFAULT-ACL change is the actual fix. The loop only cleans up relations that
-- already drifted; without the default-ACL revoke the next rebuild would undo it
-- again, exactly as it undid 331.
--
-- Guarded on PG17: MAINTAIN does not exist on the PG15 CI schema replay, where the
-- privilege name is a syntax error, so the whole block returns early there. That also
-- means this migration is inert in CI and its effect is only assertable in the live
-- lane (and, once the replay is bumped to 17, by the version-guarded test in PR-D).
--
-- SELECT is deliberately NOT touched: `authenticated` legitimately reads shared-market
-- tables, which is why migration 299 kept it.
--
-- Additive/permission-only.

begin;

-- Revoking across ~85 relations takes ACCESS EXCLUSIVE on each; fail fast rather than
-- queue behind (or ahead of) the */10 pg_cron health refresh or the 30-min map rebuild.
set local lock_timeout = '5s';

do $$
declare
  r record;
  v_left integer;
begin
  if current_setting('server_version_num')::int < 170000 then
    -- MAINTAIN is PG17+. The CI schema replay runs 15, where naming the privilege is a
    -- syntax error, so there is nothing to do and nothing to assert.
    return;
  end if;

  -- 1. The durable half: stop the default ACL handing MAINTAIN to browser roles on
  --    every newly created relation. Without this the loop below is undone by the
  --    next properties_map_mv rebuild.
  execute 'alter default privileges for role postgres in schema public '
       || 'revoke maintain on tables from anon, authenticated';

  -- 2. Clean up what already drifted. Scoped to relkind r/m/p: MAINTAIN is meaningless
  --    on a plain view and revoking it there can error.
  for r in
    select c.oid::regclass::text as rel
      from pg_class c
      join pg_namespace n on n.oid = c.relnamespace
     where n.nspname = 'public'
       and c.relkind in ('r', 'm', 'p')
       and (has_table_privilege('authenticated', c.oid, 'MAINTAIN')
         or has_table_privilege('anon', c.oid, 'MAINTAIN'))
     order by 1
  loop
    execute format('revoke maintain on %s from anon, authenticated', r.rel);
  end loop;

  -- 3. Post-condition: no browser role retains MAINTAIN on a table/matview.
  select count(*) into v_left
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'public'
     and c.relkind in ('r', 'm', 'p')
     and (has_table_privilege('authenticated', c.oid, 'MAINTAIN')
       or has_table_privilege('anon', c.oid, 'MAINTAIN'));
  if v_left > 0 then
    raise exception 'MAINTAIN still held by a browser role on % relation(s)', v_left;
  end if;
end $$;

commit;
