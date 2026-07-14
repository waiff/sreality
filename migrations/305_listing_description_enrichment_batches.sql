-- 305_listing_description_enrichment_batches.sql
-- (Renumbered from 299 — the live DB already applied 299_phase0_anon_hardening,
--  and 300-304 are taken. This migration was merged to main but never applied;
--  305 is the next free number. See the migration-number CI gate added alongside.)
-- Enrichment PR B: async bazos description enrichment via the Anthropic
-- Message Batches API (50% cheaper than the synchronous enrich_bazos.yml
-- job). Mirrors migration 098 (condition_score_batches) exactly, keyed on
-- (sreality_id, snapshot_id) per request row. Two tracking tables:
--
--   listing_description_enrichment_batches          — one row per submitted batch
--   listing_description_enrichment_batch_requests   — one row per listing in a
--                                                       batch, mapping the
--                                                       Anthropic custom_id back
--                                                       to (sreality_id, snapshot_id)
--
-- The submitter (scripts/submit_enrich_batch.py) inserts a batch row + its
-- request rows. The ingester (scripts/ingest_enrich_batch.py) polls
-- in-flight batches, and on completion writes each result through
-- toolkit.bazos_enrichment.persist_enrich_result (same cache row + gap-column
-- UPDATE as the synchronous enricher) plus one llm_calls row at the
-- batch-discounted cost. Re-ingest is idempotent: only `pending` request
-- rows are processed, and persist_enrich_result's ON CONFLICT DO NOTHING
-- cache write makes re-writes safe.
--
-- Backend-only tables (service role). RLS enabled with no policies, so
-- the anon/publishable key can't read them — same posture as llm_calls.

create table if not exists listing_description_enrichment_batches (
  id                 bigint generated always as identity primary key,
  provider           text not null default 'anthropic',
  provider_batch_id  text not null unique,
  model              text not null,
  request_count      integer not null,
  status             text not null default 'submitted'
                       check (status in (
                         'submitted', 'ended', 'ingested',
                         'failed', 'canceled', 'expired'
                       )),
  succeeded_count    integer,
  errored_count      integer,
  scored_count       integer,
  ingest_error_count integer,
  total_cost_usd     numeric(12, 6),
  submitted_at       timestamptz not null default now(),
  ended_at           timestamptz,
  ingested_at        timestamptz,
  notes              text
);

create index if not exists listing_description_enrichment_batches_status_idx
  on listing_description_enrichment_batches (status, submitted_at desc);

create table if not exists listing_description_enrichment_batch_requests (
  id           bigint generated always as identity primary key,
  batch_id     bigint not null
                 references listing_description_enrichment_batches(id) on delete cascade,
  custom_id    text not null,
  sreality_id  bigint not null,
  snapshot_id  bigint not null,
  status       text not null default 'pending'
                 check (status in ('pending', 'scored', 'errored', 'skipped')),
  error        text,
  unique (batch_id, custom_id)
);

create index if not exists listing_description_enrichment_batch_requests_batch_status_idx
  on listing_description_enrichment_batch_requests (batch_id, status);

alter table listing_description_enrichment_batches enable row level security;
alter table listing_description_enrichment_batch_requests enable row level security;
