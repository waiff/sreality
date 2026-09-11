-- 498_location_w1b_claims_slim.sql
--
-- Location simplification sprint, wave W1-b, file 2 of 2. **APPLY THIS AFTER THE
-- ROLLOUT.** Rule 25 ("one store, one lane, nine claim types, no flags; every
-- location PR deletes at least as much as it adds"): the claim spine slims to the 19
-- columns the resolver actually reads, and every side table, view and flag built
-- around the old completeness-first shape goes.
--
-- THE ORDER, and it is not negotiable (497's header carries the full argument):
--
--     apply 497  ->  merge  ->  Railway green + one green intake tick  ->  apply 498
--
-- 497 RELAXES — five CHECKs, three NOT NULLs, one DEFAULT and the payload FK — so the
-- new 19-column write is legal on the OLD table; this file DROPS, once the new code is
-- the only code running. Splitting them is what makes both windows safe: between 497
-- and 498 either version of the code writes correctly. Every drop below is `if exists`,
-- so a replay from an empty database and the live order land on the same schema even
-- where 497 got there first.
--
-- **`--retract` must not be run between the merge and this file.** `contracts.retract()`
-- DELETEs claim rows, and three of the tables dropped below still FK to
-- `location_claims(id)` until it runs. Nothing else in the code deletes a claim, so no
-- scheduled lane is exposed.
--
-- WHAT THIS DELETES, AND WHY EACH IS DEAD
--
--   location_claim_observations  263 M rows / 50 GB — the single largest relation in
--                                the subsystem. It existed so the TIME-FREE fingerprint
--                                could be safe ("values dedupe; occurrences are their own
--                                series"). Nothing has ever read it: not the resolver, not
--                                the scorecard, not an operator tool. W1-a removed its last
--                                writer (operator_corrections), so it is now write-free too.
--   location_claim_links         zero code references, ever. The resolver binds registry
--                                entities in `location_resolution_candidates`.
--   location_claim_retractions   retraction stops being an append (see below).
--   location_claim_absences      "this contract looked and found nothing", written by the
--                                deleted lanes, read by nothing.
--   location_claim_type_meta     one row per enum label, seeded from the enum itself. The
--                                three flags it carries are consulted nowhere in code.
--   location_enrichment_state    the per-lane attempt ledger of the four lanes W1-a deleted.
--   portal_payload_churn         the W2a churn instrument. It measured whether hash-gated
--                                re-mining was affordable; it is, the lane ships, and the
--                                readout was a one-off.
--
-- RETRACTION IS NO LONGER AN APPEND. `location_claims_live` existed so a retraction row
-- could un-say a claim without deleting it, and migration 404 layered the shadow flag onto
-- the same rail. Both are gone: `contracts.retract()` now DELETEs the contract version's
-- claims and enqueues their listings into `dirty_locations`, so the next drain re-resolves
-- them from what survives. A claim is evidence of what a portal said, and a contract that
-- misread the portal produced no evidence — keeping the row and teaching every reader to
-- subtract it cost three views, one flag and a correlated subquery on the hot read path.
-- The resolver reads `location_claims` directly now.
--
-- THE FINGERPRINT FUNCTION IS UNTOUCHED. `location_claim_fingerprint` (migration 386) keeps
-- all 23 arguments, and the intake keeps feeding it the same values from its row dict: the
-- readers still COMPUTE page_kind, extractor_id, extractor_version, value_norm, distance_m,
-- travel_mode, target_text, declared_confidence and legacy_source_column — they are simply
-- no longer STORED. So every fingerprint already on disk stays valid, the UNIQUE index keeps
-- deduping an incremental re-walk against it, and no corpus re-insert happens in this wave.
-- Dropping a column from the tuple would have re-dialected 5 M rows.
--
-- Nothing is created. The one `add constraint` below re-states an invariant that already
-- existed, minus a column it names.

begin;

set local lock_timeout = '5s';

------------------------------------------------------------------
-- 1. Views first — all three are `select c.*`, so they depend on every column
--    below and a bare DROP COLUMN would need CASCADE to get past them. Explicit
--    drops instead: CASCADE here would also silently take anything a later
--    migration had hung off them.
--
--    Order: the two children (382/404's location_claims_live, 404's
--    location_claims_shadow) before the parent they select from.
------------------------------------------------------------------

drop view if exists location_claims_live;
drop view if exists location_claims_shadow;
drop view if exists location_claims_unretracted;

------------------------------------------------------------------
-- 2. Inbound FKs to location_claims(id) from the contradiction ledger.
--
--    The ledger itself stays this wave (it goes in W2); only its two claim-id
--    FKs go, because `location_claims` rows are now deletable (retraction) and a
--    NO ACTION FK would turn a retraction into a constraint violation. The
--    columns keep their values — they are a historical pointer, not a join the
--    ledger's readers make.
--
--    Names are Postgres-generated from 384's inline REFERENCES.
------------------------------------------------------------------

alter table location_contradictions
  drop constraint if exists location_contradictions_served_claim_id_fkey;
alter table location_contradictions
  drop constraint if exists location_contradictions_claimed_claim_id_fkey;

------------------------------------------------------------------
-- 3. The side tables.
--
--    location_claim_observations / _links / _retractions each carry an FK to
--    location_claims(id) and _retractions one to location_claim_batches(id);
--    dropping the tables drops those constraints with them, so no CASCADE is
--    needed anywhere here.
------------------------------------------------------------------

drop table if exists location_claim_observations;
drop table if exists location_claim_links;
drop table if exists location_claim_retractions;
drop table if exists location_claim_absences;
drop table if exists location_claim_type_meta;
drop table if exists location_enrichment_state;
drop table if exists portal_payload_churn;

------------------------------------------------------------------
-- 4. location_claims: 45 columns -> 19.
--
--    KEPT (19): id, listing_id, source, claim_type, surface, extraction_method,
--    first_observed_at, contract_entry_id, value_text, value_num, value_geom,
--    value_jsonb, subject_scoped, declared_precision_label, declared_radius_m,
--    blur_evidence, claim_confidence, licence_class, claim_fingerprint.
--
--    The named constraint drops below are the six 497 handled plus
--    `loc_claim_value_present` (which 497 deliberately left standing — every claim
--    the new write emits satisfies it, so it guarded the window rather than
--    obstructing it, and it is re-stated afterwards minus value_shape). They are
--    `if exists` no-ops on the live database and the real drops on a REPLAY FROM
--    EMPTY, where 497 ran against a table 382 had just created carrying them.
--
--    Everything else that names a dropped column is dropped BY Postgres along with
--    the column (a table constraint or index "involving" a dropped column goes
--    automatically): loc_claim_evidence_payload, the travel_mode and
--    history_completeness CHECKs, location_claims_batch_fk, and the
--    location_claims_snapshot / _payload / _payload_id / _norm_trgm indexes.
------------------------------------------------------------------

alter table location_claims drop constraint if exists loc_claim_value_present;
alter table location_claims drop constraint if exists loc_claim_anchor;
alter table location_claims drop constraint if exists loc_claim_text_evidence;
alter table location_claims drop constraint if exists loc_claim_llm_model;
alter table location_claims drop constraint if exists loc_claim_legacy;
alter table location_claims drop constraint if exists loc_claim_distance_shape;
alter table location_claims drop constraint if exists location_claims_payload_id_fkey;

alter table location_claims
  drop column if exists source_id_native,
  drop column if exists snapshot_id,
  drop column if exists snapshot_anchor,
  drop column if exists payload_id,
  drop column if exists payload_sha256,
  drop column if exists extracted_at,
  drop column if exists page_kind,
  drop column if exists extractor_id,
  drop column if exists extractor_version,
  drop column if exists batch_id,
  drop column if exists value_norm,
  drop column if exists value_shape,
  drop column if exists distance_m,
  drop column if exists travel_mode,
  drop column if exists target_text,
  drop column if exists evidence_quote,
  drop column if exists span_start,
  drop column if exists span_end,
  drop column if exists payload_scope_version,
  drop column if exists model,
  drop column if exists prompt_version,
  drop column if exists declared_confidence,
  drop column if exists legacy_source_column,
  drop column if exists legacy_write_path_unknown,
  drop column if exists history_completeness,
  drop column if exists created_at;

-- The one index that survives its columns but not its purpose: `foreign_indicator`
-- and `country` are resolved per listing through location_claims_listing like every
-- other claim type, and no reader scans them corpus-wide by source.
drop index if exists location_claims_foreign;

-- Re-stated minus value_shape. NOT VALID on purpose: validating it means an
-- ACCESS EXCLUSIVE scan of the whole live claim spine to re-prove a property no
-- row can violate (the one reader that emits a shape — page_readers' geometry
-- branch — always emits value_text and value_jsonb alongside it). NOT VALID still
-- enforces every future write, which is the whole job of the constraint.
alter table location_claims
  add constraint loc_claim_value_present check (
    value_text is not null or value_num is not null or value_geom is not null
    or value_jsonb is not null) not valid;

------------------------------------------------------------------
-- 5. The contract shadow flag (migration 404).
--
--    Shadow was "claims mined and stored but excluded from resolution until a
--    frozen labelled sample clears its floors". The floors gate was never
--    exercised end to end, all nine contracts are un-shadowed, and the mechanism
--    cost a header column, two views and a dirty_locations reason.
------------------------------------------------------------------

alter table portal_contracts drop column if exists shadow;

-- 404 widened this CHECK by one value; 384's inline name is Postgres-generated and
-- deterministic. Dropped by that name WITHOUT `if exists` for 404's own reason: a
-- rename would leave the old constraint standing next to the new one.
alter table dirty_locations drop constraint dirty_locations_reason_check;
alter table dirty_locations add constraint dirty_locations_reason_check
  check (reason in
    ('claim_insert', 'resolution_written', 'registry_version', 'policy_version',
     'collision_recompute', 'property_grouping', 'operator_edit', 'full_sweep'));

commit;
