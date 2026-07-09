-- 284_candidate_archive_engine_decision.sql
-- Fix the drift that migration 272 opened: it added engine_decision +
-- last_engine_decision_at to property_identity_candidates, but the archive
-- (migration 228, built `LIKE property_identity_candidates` when it had 12 columns
-- + archived_at + archive_batch) never got them. archive_reset_candidates() then
-- did `INSERT INTO ..._archive SELECT c.*, now(), batch`, which after 272 produced
-- 16 expressions into a 14-column archive — "INSERT has more expressions than
-- target columns" (Postgres 42601). The operator's /dedup "reset candidates"
-- Maintenance action would crash; the fake-conn tests can't see it, but the
-- schema-aware SQL gate (migration/CI) does.
--
-- Add the two columns so the archive can hold every source column again. Appended
-- (positions 15-16, after archived_at/archive_batch) since Postgres can't insert
-- mid-table — which is exactly why api.property_dedup.archive_reset_candidates now
-- lists columns EXPLICITLY instead of the positional `SELECT c.*`, so physical
-- order no longer matters and a future source column can't silently misalign.

alter table property_identity_candidates_archive
  add column if not exists engine_decision text,
  add column if not exists last_engine_decision_at timestamptz;

comment on column property_identity_candidates_archive.engine_decision is
  'Mirror of property_identity_candidates.engine_decision at archive time (snapshot).';
comment on column property_identity_candidates_archive.last_engine_decision_at is
  'Mirror of property_identity_candidates.last_engine_decision_at at archive time (snapshot).';
