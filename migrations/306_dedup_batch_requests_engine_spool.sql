-- 306_dedup_batch_requests_engine_spool.sql
-- Engine-fed batch deferral (dedup-cost-reduction.md §4.1, re-decided in
-- dedup-vision-and-backlog-overhaul.md §1.2/§5): the dedup engine itself now
-- defers its OWN already-routed cold vision calls into dedup_batch_requests
-- instead of a second process (the old submit_dedup_batch collect() funnel)
-- re-deriving the work-list. A deferred request is written with batch_id NULL
-- (the "spool"); scripts/submit_dedup_batch.py's new job is to periodically
-- flush the spool — chunk unsubmitted rows into provider Batch API calls, then
-- backfill batch_id — instead of walking populations to guess what to warm.
--
-- request_params stores the FULLY BUILT provider-shaped request body (the same
-- dict provider.build_batch_request_params returns) at defer time, so flush
-- never re-fetches R2 image bytes or re-derives a request — it only submits
-- exactly what the engine already built. queued_at orders the flush query.
--
-- The partial unique index on custom_id WHERE batch_id IS NULL makes spooling
-- idempotent across concurrent lane passes without a second table: a pair two
-- different sweep lanes both defer in the same window enqueues once.

alter table dedup_batch_requests
  alter column batch_id drop not null,
  add column request_params jsonb,
  add column queued_at timestamptz not null default now();

create unique index dedup_batch_requests_unsubmitted_custom_id_uq
  on dedup_batch_requests (custom_id)
  where batch_id is null;

create index dedup_batch_requests_unsubmitted_queued_at_idx
  on dedup_batch_requests (queued_at)
  where batch_id is null;
