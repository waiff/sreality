-- 348_listings_pk_swap_catchup.sql
-- CATCH-UP MIGRATION — A NO-OP AGAINST PRODUCTION. Do not apply it there.
--
-- R2 GATE 1 (docs/design/listing-identity-r2-pk-swap-runbook.md §6) swapped
-- listings' PRIMARY KEY from sreality_id onto the surrogate id. It ran LIVE,
-- out-of-band, through scripts/apply_listings_pk_swap.py + the dispatch-only
-- apply_listings_pk_swap.yml workflow (PR #887) and was never written as a
-- numbered file, so migrations/ and the live database silently diverged:
-- 001_initial.sql still declares `sreality_id bigint primary key`, and the CI
-- schema replay (.github/workflows/migrations.yml) therefore rebuilds a schema
-- whose PK is on sreality_id while prod's is on id.
--
-- Why that has to be fixed now: the Gate 2 migration needs sreality_id nullable
-- (new non-sreality rows will insert NULL). Against the replayed schema it would
-- abort with "column sreality_id is in a primary key" — a hard CI failure on a
-- change that is a no-op against prod. This file makes replay agree with reality.
--
-- Every step is guarded on the live catalog, so applying this to production
-- anyway changes nothing and takes NO lock on `listings` (566k rows, always-on
-- writer) — the guard short-circuits before any ALTER. Re-runnable.
--
-- End state, mirrored verbatim from the live database (verified 2026-07-21):
--   listings_pkey             PRIMARY KEY (id); the index is listings_id_pk_idx
--                             renamed by the USING INDEX promotion
--   sreality_id               nullable ordinary column, still populated everywhere
--   listings_id_key           UNIQUE (id) — migration 313, unchanged
--   listings_sreality_id_uidx UNIQUE (sreality_id) — migration 337, the rollback
--                             lever and the backing index for ON CONFLICT
--                             (sreality_id); deliberately kept
--   19 child FKs on listings(id), 0 on listings(sreality_id) — migration 338
-- listings_sreality_id_sign_check (migration 311) was already written in the
-- forward-compatible form that permits a NULL sreality_id on a non-sreality row,
-- so Gate 2 needs no change to it.

SET lock_timeout = '5s';

DO $$
DECLARE
    pk_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO pk_def
      FROM pg_constraint
     WHERE conrelid = 'listings'::regclass AND contype = 'p';

    IF pk_def IS NULL OR pk_def LIKE '%sreality_id%' THEN
        -- 337 already built this index on a fresh rebuild; the CREATE is the
        -- belt-and-braces arm for a chain replayed without it. It never runs
        -- against prod, where the guard above is already false.
        CREATE UNIQUE INDEX IF NOT EXISTS listings_id_pk_idx ON listings (id);

        -- One transaction (the DO block's), exactly as the live window ran it:
        -- between the DROP and the ADD there is no unique index backing
        -- ON CONFLICT (sreality_id), and a writer that planned in that gap would
        -- abort with 42P10.
        ALTER TABLE listings DROP CONSTRAINT IF EXISTS listings_pkey;
        ALTER TABLE listings
            ADD CONSTRAINT listings_pkey PRIMARY KEY USING INDEX listings_id_pk_idx;
    END IF;

    -- Separate step, separately guarded: dropping a PRIMARY KEY leaves the
    -- columns' attnotnull set, so sreality_id stays NOT NULL until told otherwise.
    IF EXISTS (
        SELECT 1 FROM pg_attribute
         WHERE attrelid = 'listings'::regclass
           AND attname = 'sreality_id'
           AND attnotnull
    ) THEN
        ALTER TABLE listings ALTER COLUMN sreality_id DROP NOT NULL;
    END IF;
END $$;

RESET lock_timeout;
