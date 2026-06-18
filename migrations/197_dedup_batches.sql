-- 197_dedup_batches.sql
-- Async dedup VISION via the Anthropic Message Batches API (50% cheaper than
-- the synchronous dedup engine's per-call vision, recall-identical). Two
-- tracking tables, mirroring condition_score_batches (migration 098):
--
--   dedup_batches          — one row per submitted batch
--   dedup_batch_requests   — one row per vision request in a batch, mapping the
--                            Anthropic custom_id back to (kind, listing(s), room,
--                            model) so the ingester can persist to the right cache
--
-- The lane PRE-WARMS the dedup engine's vision caches; it never merges. The
-- submitter (scripts/submit_dedup_batch.py) runs the engine's FREE funnel
-- (rules A/B/C + the pHash fast-path + the cross-source gate) to find the
-- cross-source pairs that would reach the paid visual stage, then enqueues their
-- classify / compare / site_plan requests (only those not already cached). The
-- ingester (scripts/ingest_dedup_batch.py) polls in-flight batches and, per
-- request, writes the result through the owning toolkit module's persist helper
-- (image_room_classifications / listing_visual_matches / listing_site_plan_matches —
-- the SAME cache rows the synchronous tools write) plus one llm_calls row at the
-- 50% batch-discounted cost. The daily dedup_engine.yml run then REPLAYS unchanged
-- over the warm caches: every classify/compare is a cache hit, so it produces the
-- identical merges for free (a cache miss falls back to a synchronous call — still
-- correct, just not discounted).
--
-- Unlike condition scoring, dedup requests mix MODELS within one batch (classify
-- on Haiku, compare/site_plan on Sonnet — Anthropic allows per-request models),
-- so the model lives on the REQUEST row, not the batch. `image_ids` records the
-- ordered images a classify request sent, so the ingester maps the tool-call's
-- 0-based index back to the right image_id even if the listing's image set shifts
-- between submit and ingest.
--
-- Re-ingest is idempotent: only `pending` request rows are processed, and every
-- cache the persist helpers write is keyed (image_id/pair+room/pair) + ON CONFLICT
-- upsert, so a re-run after a partial ingest finishes the rest safely.
--
-- Backend-only tables (service role). RLS enabled with no policies, so the
-- anon/publishable key can't read them — same posture as condition_score_batches.

create table dedup_batches (
  id                 bigint generated always as identity primary key,
  provider           text not null default 'anthropic',
  provider_batch_id  text not null unique,
  request_count      integer not null,
  status             text not null default 'submitted'
                       check (status in (
                         'submitted', 'ended', 'ingested',
                         'failed', 'canceled', 'expired'
                       )),
  succeeded_count    integer,
  errored_count      integer,
  ingested_count     integer,
  ingest_error_count integer,
  total_cost_usd     numeric(12, 6),
  submitted_at       timestamptz not null default now(),
  ended_at           timestamptz,
  ingested_at        timestamptz,
  notes              text
);

create index on dedup_batches (status, submitted_at desc);

create table dedup_batch_requests (
  id            bigint generated always as identity primary key,
  batch_id      bigint not null
                  references dedup_batches(id) on delete cascade,
  custom_id     text not null,
  kind          text not null
                  check (kind in ('classify', 'compare', 'site_plan')),
  model         text not null,
  sreality_id_a bigint not null,
  sreality_id_b bigint,            -- null for classify (single-listing)
  room_type     text,              -- compare only
  image_ids     bigint[],          -- classify: ordered images sent (index -> image_id)
  status        text not null default 'pending'
                  check (status in ('pending', 'done', 'errored', 'skipped')),
  error         text,
  unique (batch_id, custom_id)
);

create index on dedup_batch_requests (batch_id, status);

-- In-flight guard: submit skips a request whose custom_id is still pending on a
-- non-terminal batch, so overlapping submit runs never double-bill one vision call.
create index on dedup_batch_requests (custom_id) where status = 'pending';

alter table dedup_batches enable row level security;
alter table dedup_batch_requests enable row level security;
