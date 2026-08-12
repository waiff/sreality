-- CI-only bootstrap for the migration smoke-test (.github/workflows/migrations.yml).
-- NOT part of the tracked schema. It provisions what Supabase provides in
-- production but a vanilla postgis/postgis container does not, so the full
-- migrations/ chain applies cleanly:
--   * the Supabase roles the migrations GRANT to / write RLS policies for
--   * the pg_trgm extension that migrations 067 / 074 rely on for trigram
--     indexes but never `create`. (postgis is already enabled by the container
--     image; we create both idempotently here for good measure.)
-- The service container is fresh on every run, so these are safe.
create extension if not exists postgis;
create extension if not exists pg_trgm;

create role anon nologin;
create role authenticated nologin;
create role service_role nologin bypassrls;
-- PostgREST's connection role: migration 394 pins pgrst.db_max_rows on it.
create role authenticator nologin;

-- Supabase's public-schema DEFAULT ACL, which a vanilla container has NOT got.
-- In production `pg_default_acl` carries a postgres/public/TABLES entry, so every
-- relation a migration creates is born with a browser-role SELECT grant that no
-- migration ever writes — that invisible grant is how firms_public (migration 395)
-- and listings_public/properties_public (migration 398) came to be readable by any
-- logged-in session. Without it here the replay starts from an all-empty ACL, every
-- `has_table_privilege('authenticated', ...)` assertion is vacuously satisfied, and
-- the whole default-ACL reachability class is unassertable in CI.
--
-- SELECT only, granted BEFORE the chain runs, so 299 PART A/B's revokes act on a
-- real baseline: PART A trims the default to `authenticated=r` (production's exact
-- live value) and PART B clears anon off the pre-299 objects. Write privileges are
-- deliberately not seeded — production's writes were revoked long ago and seeding
-- them would assert a state that no longer exists.
alter default privileges for role postgres in schema public
  grant select on tables to anon, authenticated;

-- Supabase provides the `auth` schema + `auth.users` in production. A vanilla
-- container does not, so migrations that FK-reference or trigger on auth.users
-- (multi-tenant accounts, migration 286+) fail the smoke-test without this stub.
-- Minimal shape sufficient for FK targets + AFTER INSERT triggers.
create schema if not exists auth;
create table if not exists auth.users (
  id    uuid primary key default gen_random_uuid(),
  email text
);
