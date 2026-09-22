-- Re-key listing_description_enrichments onto the TEXT, not the snapshot.
-- Field-capture W7, approved destructive step (ii) (operator OK 2026-09-21).
--
-- DESTRUCTIVE: drops a UNIQUE constraint on a 37,754-row table.
--   Backup first (the whole table, it is small):
--     pg_dump "$SUPABASE_DB_URL" -t listing_description_enrichments \
--       --data-only -f lde_before_549.sql
--
-- WHY. The old key was (sreality_id, snapshot_id, model), and each of its three columns
-- was a defect:
--   * `sreality_id` is NULL on 49,982 of 50,205 active bazos rows since listing-identity
--     Gate 2 flipped (2026-07-23), so the lane that read this cache selected 223 rows and
--     reported green for two months. `listing_id` (migration 321) is on all 37,754 rows.
--   * `snapshot_id` made the cache a function of the whole listing: a price-only snapshot
--     re-billed the identical description. ~$65 of the lane's ~$207 lifetime spend was
--     that, and 6,649 listings were extracted two or more times. R6: a text cell is a pure
--     function of `description`, so the key is the hash of `description`.
--   * `model` is not deleted, it MOVES: `extractor_version` is
--     '<schema>:<open-gate hash>:<model>'
--     (toolkit/description_extraction.extractor_version), so migration 249's lesson — a
--     model upgrade must re-attempt, a same-model re-run must not re-bill — survives, and
--     a change to the tool schema, the merge rules or the set of fields the lane is
--     allowed to write invalidates the cache too, which the bare model id could not
--     express. That last part is load-bearing: the lane asks only for the fields whose R7
--     gate is open, so a row cached while three gates were open is not an answer for the
--     fourth, and without the fingerprint the anti-join would retire that listing for ever
--     and the newly-opened column would stay NULL on the whole existing corpus.
--
-- THE 37,754 EXISTING ROWS STAY, as history, with text_hash and extractor_version NULL.
-- They are not backfillable and must not be faked: the hash would have to be of the
-- description AS IT WAS when the row was written, and `listings.description` is
-- latest-wins with no history of its own. NULL is also the correct behaviour — a unique
-- index treats NULLs as distinct, so an old row never blocks an insert, and the new
-- lane's selector only matches a row whose text_hash equals today's description, so an
-- old row is never a cache HIT either. Those rows are the OLD extractor's output, at the
-- precision W1 refuted; the new lane re-reads a listing at most once per extractor
-- version, and today not at all, because every R7 gate ships closed and a closed gate is
-- outside the lane's scope entirely.
--
-- NOTHING IN THE RUNTIME READS THESE COLUMNS UNTIL A GATE OPENS: with every gate closed
-- the lane and `verify_pipeline`'s `text_extraction_lag` both return before they query.
-- Apply this before opening any gate; applying it before the merge costs nothing.
--
-- snapshot_id loses its NOT NULL for the same reason the key drops it: the new writer has
-- no snapshot to name and must not invent one.
--
-- THERE ARE TWO OLD KEYS, NOT ONE, and only one of them is in this directory. Live the
-- table also carries `listing_description_enrichments_lid_snap_model_key`
-- UNIQUE (listing_id, snapshot_id, model) — the R2 surrogate twin, created by the Phase-B
-- apply SCRIPT rather than by a numbered migration, so a schema replayed from zero does
-- not have it. Both drops are therefore IF EXISTS, and both must happen: leaving the twin
-- would keep a `snapshot_id`-keyed uniqueness on a table whose new writer never sets one.
--
-- COUNTS (measured 2026-09-22, before):
--   listing_description_enrichments            37,754 rows
--   ... with listing_id NOT NULL               37,754 (100 %)
--   ... distinct listing_id                    30,554
--   ... with text_hash / extractor_version          0 (the columns do not exist yet)
-- AFTER: 37,754 rows, unchanged; two new NULL columns; two UNIQUE constraints dropped,
-- one added, one now-redundant index dropped; no row is inserted, updated or deleted here.

ALTER TABLE listing_description_enrichments
  ADD COLUMN IF NOT EXISTS text_hash text,
  ADD COLUMN IF NOT EXISTS extractor_version text;

ALTER TABLE listing_description_enrichments
  ALTER COLUMN snapshot_id DROP NOT NULL;

-- Column order is the selector's, not the key's reading order: the lane's anti-join
-- probes (listing_id, extractor_version) and compares text_hash as a FILTER, so that a
-- listing with no extraction at this version never has its description detoasted and
-- hashed. With text_hash second the planner would have to hash all ~50k descriptions on
-- every pass, forever.
ALTER TABLE listing_description_enrichments
  ADD CONSTRAINT listing_description_enrichments_text_key
  UNIQUE (listing_id, extractor_version, text_hash);

ALTER TABLE listing_description_enrichments
  DROP CONSTRAINT IF EXISTS listing_description_enrichments_sid_snapshot_model_key;

ALTER TABLE listing_description_enrichments
  DROP CONSTRAINT IF EXISTS listing_description_enrichments_lid_snap_model_key;

-- Redundant the moment the new key exists: same leading column, one level narrower.
DROP INDEX IF EXISTS listing_description_enrichments_listing_id_idx;
