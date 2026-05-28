-- 108_detail_queue_source_generic.sql
--
-- Phase 4 (slice C): re-key listing_detail_queue (migration 105) from a
-- sreality-only `sreality_id bigint` PK to a source-generic `(source, native_id)`
-- key, so EVERY portal's index-walk can enqueue into the one shared queue and the
-- one shared detail-drain can claim from it. Adds:
--   * native_id  — the portal-native id (sreality: sreality_id::text; bazos: the
--     listing's source_id_native). The stable per-portal identity at enqueue time.
--   * detail_ref — what the drain needs to FETCH the detail. sreality derives the
--     URL from sreality_id so leaves it NULL; bazos stores the detail path/URL.
--
-- BACKWARD-COMPATIBLE so the currently-deployed (main) sreality code keeps working
-- until the new code merges: sreality_id stays a UNIQUE index (so the old
-- `ON CONFLICT (sreality_id)` enqueue is still valid), just no longer the PK
-- (bazos rows carry a NULL sreality_id until ingest allocates a synthetic one).
-- No rows are lost — existing sreality rows backfill native_id = sreality_id::text.
--
-- Authorized queue re-key (operator OK). The queue is a regenerable work signal
-- (the index-walk re-enqueues), not history.

alter table listing_detail_queue add column native_id  text;
alter table listing_detail_queue add column detail_ref text;

-- Every existing row is sreality; its native id is the sreality_id as text.
update listing_detail_queue set native_id = sreality_id::text where native_id is null;
alter table listing_detail_queue alter column native_id set not null;

-- Swap the PK for a plain UNIQUE on sreality_id: keeps `ON CONFLICT (sreality_id)`
-- valid for the old code path, but allows NULL (bazos has no sreality_id at
-- enqueue; multiple NULLs are distinct in a unique index).
alter table listing_detail_queue drop constraint listing_detail_queue_pkey;
alter table listing_detail_queue alter column sreality_id drop not null;
create unique index listing_detail_queue_sreality_id_key
  on listing_detail_queue (sreality_id);

-- The new source-generic identity + claim key.
create unique index listing_detail_queue_source_native_key
  on listing_detail_queue (source, native_id);

-- Source-scoped claimable index: a drain claims one portal's rows, highest
-- priority + oldest first.
drop index if exists listing_detail_queue_claimable_idx;
create index listing_detail_queue_claimable_idx on listing_detail_queue
  (source, priority desc, enqueued_at)
  where claimed_at is null and given_up = false;
