-- 025_broker_fields.sql
-- Add the three broker contact columns scraped from sreality detail
-- pages. Captured retroactively as a documentation file: the live
-- production DB had this applied (supabase migration version
-- 20260510153221) but the migrations/ folder was missing the file.
-- Recovered verbatim from supabase_migrations.schema_migrations so
-- a fresh rebuild from migrations/ reproduces the live schema.
--
-- Coexists alphabetically with 025_curation_public_views.sql in the
-- same numeric prefix bucket — same pattern the repo already uses
-- for 022_*, 023_*, 024_*, 028_*.

alter table listings
  add column if not exists broker_name  text,
  add column if not exists broker_email text,
  add column if not exists broker_phone text;
