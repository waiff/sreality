-- Dedup v2 Phase 4: candidate backup before a reset ("disregard candidates, keep
-- a backup, redo all"). The operator archives the current proposed queue here
-- before re-running the engine fresh (e.g. at the CLIP flip) — the engine then
-- regenerates clean candidates. Merges/dismissals are NOT touched (they live in
-- property_merge_events / the property rows); only the transient review QUEUE is
-- snapshotted + cleared.
--
-- Columns mirror property_identity_candidates (LIKE — no constraints, so the same
-- candidate id can be archived across several reset batches), + archived_at and a
-- batch label. No PK: it's an append-only snapshot store.

create table if not exists property_identity_candidates_archive (
  like property_identity_candidates
);

alter table property_identity_candidates_archive
  add column if not exists archived_at   timestamptz not null default now(),
  add column if not exists archive_batch  text;

create index if not exists pic_archive_batch_idx
  on property_identity_candidates_archive (archive_batch);
