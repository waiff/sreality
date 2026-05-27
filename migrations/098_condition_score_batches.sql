-- 098_condition_score_batches.sql
-- Phase 1.8b: async condition scoring via the Anthropic Message Batches
-- API (50% cheaper than synchronous calls). Two tracking tables:
--
--   condition_score_batches          — one row per submitted batch
--   condition_score_batch_requests   — one row per listing in a batch,
--                                       mapping the Anthropic custom_id
--                                       back to (sreality_id, snapshot_id)
--
-- The submitter (scripts/submit_condition_batch.py) inserts a batch row +
-- its request rows. The ingester (scripts/ingest_condition_batch.py) polls
-- in-flight batches, and on completion writes each result through
-- toolkit.condition_scoring.persist_scoring_result (same cache row +
-- guarded listings.* UPDATE as the synchronous scorer) plus one llm_calls
-- row at the batch-discounted cost. Re-ingest is idempotent: only
-- `pending` request rows are processed, and the (sreality_id, snapshot_id)
-- cache key + latest-wins guard make re-writes safe.
--
-- Backend-only tables (service role). RLS enabled with no policies, so
-- the anon/publishable key can't read them — same posture as llm_calls.

create table condition_score_batches (
  id                 bigint generated always as identity primary key,
  provider           text not null default 'anthropic',
  provider_batch_id  text not null unique,
  model              text not null,
  n_images           integer not null default 0,
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

create index on condition_score_batches (status, submitted_at desc);

create table condition_score_batch_requests (
  id           bigint generated always as identity primary key,
  batch_id     bigint not null
                 references condition_score_batches(id) on delete cascade,
  custom_id    text not null,
  sreality_id  bigint not null,
  snapshot_id  bigint not null,
  status       text not null default 'pending'
                 check (status in ('pending', 'scored', 'errored', 'skipped')),
  error        text,
  unique (batch_id, custom_id)
);

create index on condition_score_batch_requests (batch_id, status);

alter table condition_score_batches enable row level security;
alter table condition_score_batch_requests enable row level security;
