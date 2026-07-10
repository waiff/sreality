-- 277_candidates_archive_engine_columns.sql
-- Mirror migration 272's two engine-bookkeeping columns onto the candidates ARCHIVE.
--
-- WHY: property_identity_candidates_archive was created `LIKE property_identity_candidates`
-- (migration 228) and archive_reset_candidates (api/property_dedup.py) archived with a
-- POSITIONAL `INSERT ... SELECT c.*, now(), %s` that relied on the archive's column order
-- being (candidate cols…, archived_at, archive_batch). Migration 272 widened the source
-- table (engine_decision, last_engine_decision_at) without widening the archive, so the
-- positional INSERT has had more expressions than target columns since 2026-07-05 — the
-- operator's "archive + reset queue" action would 42601 at runtime. Caught by the
-- schema-aware SQL gate (tests/test_sql_schema_prepare.py) on its first run after landing.
--
-- Postgres can only APPEND columns, so the archive's order is now (candidate cols…,
-- archived_at, archive_batch, engine_decision, last_engine_decision_at) — permanently
-- different from the source order. The positional INSERT is therefore retired in the same
-- change: api/property_dedup.py switches to explicit column lists, immune to order drift.
-- Any FUTURE widening of property_identity_candidates must add the same columns here and
-- extend that explicit list — the SQL gate enforces this automatically now.

alter table property_identity_candidates_archive
  add column if not exists engine_decision         text,
  add column if not exists last_engine_decision_at timestamptz;
