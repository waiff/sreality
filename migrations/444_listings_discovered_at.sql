-- 444: separate "when we FOUND it" from "when we SAVED it".
--
-- `listings.first_seen_at` is set by the drain at write time, so it has always meant
-- "when the detail fetch landed" while every consumer reads it as "when the listing
-- appeared". While the queue was healthy those two were minutes apart and the conflation
-- was invisible. On 2026-08-17 the detail queue began starving its new-listing class
-- (fixed in #1196/#1197) and the gap opened to NINE DAYS -- 15,064 listings discovered and
-- unsaved, median wait 2.7 days. Every series built on first_seen_at silently absorbed
-- that backlog: days-on-market, listing velocity, the price-drop baseline, and the
-- watchdog's ":new:" event all read a queue delay as market behaviour.
--
-- The honest discovery time already exists and is already durable -- `listing_detail_queue.
-- enqueued_at`, which enqueue_detail deliberately PRESERVES across re-sightings (see its
-- docstring). It is simply destroyed when the row is deleted on success. This column keeps
-- it, stamped from the claim on the same set-once path migration 368 built for
-- `discovery_seq`: COALESCE(listings.discovered_at, EXCLUDED.discovered_at), so a refetch's
-- fresh queue draw can never overwrite a first discovery.
--
-- ---------------------------------------------------------------------------------------
-- NO BACKFILL FROM first_seen_at. DELIBERATELY.
--
-- The obvious "backfill discovered_at = first_seen_at for history" would launder exactly
-- the bias this column exists to expose: it would assert that the 15,064 starved listings
-- were discovered the moment they were saved, which is the false statement that hid the
-- outage for nine days. NULL is the truthful value for a row whose discovery time we did
-- not keep, and a NULL is visible to an analyst in a way a plausible-but-wrong timestamp
-- is not.
--
-- `detail_queue_completions` (migration 265) holds real enqueued_at -> completed_at pairs
-- for its retention window and can seed the recent tail; that runs as an explicit,
-- verified statement AFTER this migration, never inside it -- `listings` is a hot,
-- heavily-TOASTed table where an unbounded UPDATE inside a migration is how you get a
-- transaction that times out at the pooler and takes the write path with it.
--
-- ADD COLUMN ... NULL with no DEFAULT is catalog-only in PG11+ (no table rewrite), so the
-- ACCESS EXCLUSIVE lock is held for microseconds. It still has to WIN that lock against a
-- continuous drain, so apply it while the write path is idle.

ALTER TABLE public.listings ADD COLUMN IF NOT EXISTS discovered_at timestamptz;

COMMENT ON COLUMN public.listings.discovered_at IS
    'When the index walk first SAW this listing (listing_detail_queue.enqueued_at, carried '
    'through the claim). Set once, never overwritten by a refetch. Distinct from '
    'first_seen_at, which is when the detail fetch was WRITTEN -- the two diverge by exactly '
    'the queue wait, which reached 9 days in the 2026-08-17 starvation incident. NULL means '
    'the discovery time was not retained (all rows written before migration 444, minus the '
    'tail seeded from detail_queue_completions); it is deliberately NOT backfilled from '
    'first_seen_at, which would restate a queue delay as a market fact.';
