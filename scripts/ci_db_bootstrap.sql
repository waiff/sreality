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

-- Supabase provides the `auth` schema + `auth.users` in production. A vanilla
-- container does not, so migrations that FK-reference or trigger on auth.users
-- (multi-tenant accounts, migration 286+) fail the smoke-test without this stub.
-- Minimal shape sufficient for FK targets + AFTER INSERT triggers.
create schema if not exists auth;
create table if not exists auth.users (
  id    uuid primary key default gen_random_uuid(),
  email text
);
