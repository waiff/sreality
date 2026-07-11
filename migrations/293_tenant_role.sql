-- 293_tenant_role.sql
-- Phase 1 increment 3, part 4/6 — the non-superuser tenant login role.
--
-- The API talks to Postgres directly via psycopg (never PostgREST), and
-- Supabase's authenticated/anon roles are NOLOGIN — so the tenant pool
-- connects as this dedicated LOGIN role and does `SET LOCAL ROLE
-- authenticated` + set_config('request.jwt.claims', ..., true) inside each
-- request's single transaction (Amendment A1; api/tenant_pool.py). NOINHERIT
-- means membership grants nothing until that explicit SET ROLE — the role has
-- zero data access on its own, so a forgotten SET ROLE fails closed.
--
-- No password here (secrets discipline): applied with PASSWORD NULL (login
-- refused until set); the operator sets one via the Supabase SQL editor
-- (ALTER ROLE tenant_pool PASSWORD '...') and stores it only in Railway as
-- part of TENANT_POOL_DB_URL.
--
-- Guarded create + current_database(): roles are cluster-wide and the CI
-- replay container's database is not named `postgres`.

begin;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'tenant_pool') then
    create role tenant_pool with
      login
      nosuperuser
      nocreatedb
      nocreaterole
      nobypassrls
      noinherit
      password null
      connection limit 50;
  end if;
end $$;

grant authenticated to tenant_pool;

do $$
begin
  execute format('grant connect on database %I to tenant_pool', current_database());
end $$;

grant usage on schema public to tenant_pool;

commit;
