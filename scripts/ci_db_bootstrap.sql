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
