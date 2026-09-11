-- 497_location_w1b_claims_relax.sql
--
-- Location simplification sprint, wave W1-b, file 1 of 2. **APPLY THIS BEFORE THE CODE
-- MERGES.** Metadata-only: no table is rewritten, no row is read, no object is created.
--
-- THE WINDOW THIS CLOSES
--
-- W1-b slims `location_claims` from 45 columns to 19 (file 2, migration 498). The code and
-- the schema cannot change in the same instant: a merge deploys Railway and the next hourly
-- `location_claims_intake.yml` tick within minutes, while a migration is applied by hand.
-- So there are two windows, and a single destructive migration can only ever protect one:
--
--   (A) NEW CODE on the OLD schema — between the merge and the apply. The 19-column INSERT
--       omits five columns the old table demands and leaves five old CHECKs unsatisfiable:
--         * NOT NULL with no default: source_id_native, extractor_id, extractor_version
--           -> 23502 not_null_violation on EVERY claim written.
--         * snapshot_anchor NOT NULL DEFAULT 'snapshot' + `loc_claim_anchor`, which requires
--           snapshot_id NOT NULL whenever the anchor is 'snapshot'. The default fires, the
--           CHECK fails -> 23514 on EVERY claim.
--         * `loc_claim_text_evidence`: a `regex_text` claim must carry evidence_quote,
--           span_start, span_end, payload_scope_version and subject_scoped. Seven live
--           contract entries are regex_text; the new INSERT stores none of those.
--         * `loc_claim_legacy`: a `legacy_column` claim must name legacy_source_column.
--           Twenty-four live entries are legacy_column.
--         * `loc_claim_distance_shape`: relative_distance / poi_distance need distance_m
--           and target_text.
--       Net effect without this file: the hourly intake writes ZERO claims and fails loudly
--       once an hour until someone applies the drops.
--
--   (B) OLD CODE on the NEW schema — if the drops were applied first and the deploy lagged.
--       The old INSERT names dropped columns -> 42703 undefined_column, same outage.
--
-- This file makes window (A) safe and window (B) irrelevant: it only RELAXES. Every
-- statement below is legal for the old code too (it writes the columns, they are merely no
-- longer compulsory), so the two halves commute and the order is simply
--
--     apply 497  ->  merge  ->  Railway green + one green intake tick  ->  apply 498
--
-- THE SECOND HALF: THE PAYLOAD FK (the same window, a quieter failure)
--
-- `payloads._REPIN_SQL` stops pinning bodies a claim points at, so `_PRUNE_SQL` starts
-- evicting mid-versions that `location_claims.payload_id` still references. That FK is NO
-- ACTION, so on the old schema the DELETE raises 23503 and rolls back the WHOLE bounded
-- append transaction — losing the body just fetched and every later append for that group.
-- `scraper.db.append_payload_if_enabled` catches everything and warns, so it is one WARN
-- line per detail fetch while the archive silently stops growing and the R2 object leaks.
-- Dropping the constraint here is what makes the new pin predicate safe to deploy.
--
-- WHAT THIS FILE DOES NOT COVER, and the operational rule that covers it instead:
-- `location_claim_observations`, `location_claim_links` and `location_claim_retractions`
-- also FK to `location_claims(id)`, and W1-b's `contracts.retract()` DELETEs claim rows.
-- Those tables go in 498, so **`--retract` must not be run between the merge and 498** — a
-- retraction in that window can raise 23503 against a row in one of them. Nothing else in
-- the code deletes a claim, so no scheduled lane is exposed.

begin;

set local lock_timeout = '5s';

------------------------------------------------------------------
-- 1. The five CHECKs the 19-column write cannot satisfy.
--
-- Dropped BEFORE the NOT NULL relaxations below, so `loc_claim_anchor` cannot re-fire on
-- snapshot_anchor's surviving default while this transaction is mid-flight.
--
-- `loc_claim_value_present` and `loc_claim_coordinate_shape` are deliberately NOT here:
-- every claim the new write emits satisfies both, and they are the two invariants worth
-- keeping through the window (498 re-states the first minus `value_shape`).
------------------------------------------------------------------

alter table location_claims drop constraint if exists loc_claim_anchor;
alter table location_claims drop constraint if exists loc_claim_text_evidence;
alter table location_claims drop constraint if exists loc_claim_llm_model;
alter table location_claims drop constraint if exists loc_claim_legacy;
alter table location_claims drop constraint if exists loc_claim_distance_shape;

------------------------------------------------------------------
-- 2. The NOT NULLs with no default.
--
-- `snapshot_anchor` loses its DEFAULT as well as its NOT NULL. With `loc_claim_anchor`
-- gone the default is harmless, but leaving it would stamp 'snapshot' onto rows that are
-- not snapshot-anchored for as long as the column survives — a wrong fact is worse than a
-- NULL, and the column is about to be dropped anyway.
--
-- ALTER ... DROP NOT NULL and DROP DEFAULT are catalog-only: no scan, no rewrite. They take
-- ACCESS EXCLUSIVE for the instant it takes to update pg_attribute, which is what the
-- lock_timeout above bounds.
------------------------------------------------------------------

alter table location_claims
  alter column source_id_native  drop not null,
  alter column extractor_id      drop not null,
  alter column extractor_version drop not null,
  alter column snapshot_anchor   drop not null,
  alter column snapshot_anchor   drop default;

------------------------------------------------------------------
-- 3. The payload FK.
--
-- 382 declares `payload_id bigint references portal_raw_payloads(id)` inline, so the
-- constraint carries PostgreSQL's generated name. NOT the same object as migration 403's
-- INDEX `location_claims_payload_id`, which stays until 498 drops the column under it.
------------------------------------------------------------------

alter table location_claims
  drop constraint if exists location_claims_payload_id_fkey;

commit;
