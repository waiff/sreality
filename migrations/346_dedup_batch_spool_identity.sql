-- 346_dedup_batch_spool_identity.sql
-- R2 dedup-identity-chain PR2 of 4 (docs/design/listing-identity-r2-pk-swap-runbook.md
-- §4 item 1). Makes the dedup batch spool able to carry a listing that has NO legacy
-- sreality_id, so the spool stops depending on the pre-flip identity.
--
-- WHY THE GUARD BELOW EXISTS. `dedup_batch_requests.custom_id` is a PERSISTED unique
-- key and the spool's whole idempotency mechanism (toolkit/dedup_batch_defer.py checks
-- `custom_id = %s AND status = 'pending'` before building a request). PR2 changes that
-- key's scheme from legacy-id-derived (`cmp-<a>-<b>-<room>`) to surrogate-derived
-- (`cmpL-<a>-<b>-<room>`). Any request still `pending` across that deploy would not be
-- recognised by the new-scheme guard and would be re-spooled and RE-BILLED — provider
-- batches take up to 24h to return, so "pending" is a real, long window.
--
-- Verified live before writing this migration: the spool is EMPTY (0 rows with
-- batch_id IS NULL, 0 rows with status='pending'; all 18,250 historical rows are
-- terminal done/errored, last batch ingested 2026-07-16) AND the gate that fills it,
-- app_settings `dedup_engine_batch_defer_enabled`, has no row and defaults to false in
-- toolkit/dedup_settings.py — so the engine has never deferred and nothing can be in
-- flight. That is exactly why the scheme change is safe to make NOW rather than
-- needing a spool-flush choreography. The guard turns that precondition into something
-- executable instead of something remembered: if rows are pending when this is applied
-- (someone flipped the gate first), it aborts loudly rather than opening a silent
-- double-bill window.
--
-- The relaxation itself: `sreality_id_a` was NOT NULL, which post-Gate-2 would hard-fail
-- every defer of a non-sreality listing. It stays populated for as long as Gate 1's
-- invariant holds (every row has a legacy id); this only stops the column from being a
-- wall the moment that stops being true.

DO $$
DECLARE
    pending_count integer;
BEGIN
    SELECT count(*) INTO pending_count
    FROM dedup_batch_requests
    WHERE status = 'pending';

    IF pending_count > 0 THEN
        RAISE EXCEPTION
            'dedup_batch_requests has % pending request(s); the PR2 custom_id scheme '
            'change would re-spool and double-bill them. Run the dedup_batches workflow '
            'in ingest mode until none are pending, then re-apply.', pending_count;
    END IF;
END
$$;

ALTER TABLE dedup_batch_requests
    ALTER COLUMN sreality_id_a DROP NOT NULL;
