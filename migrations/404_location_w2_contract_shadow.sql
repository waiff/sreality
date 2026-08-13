-- 404_location_w2_contract_shadow.sql
--
-- Location-data program, Wave W2, PR W2-4: the contract SHADOW mechanism.
--
-- Design: design/final/06-migration-backfill.md section 6.4.0(2) - "A contract
-- that cannot meet them ships in *shadow* (claims written, excluded from
-- resolution) until it can", where "them" is the frozen-sample precision floor
-- (street >= 95 %, obec/okres >= 98 %, precision class >= 95 %). Every per-portal
-- W2 contract merges IN SHADOW and is un-shadowed only once its labelled sample
-- clears those floors, so the landing place has to exist BEFORE the first portal
-- contract lands - otherwise a failing gate has nowhere to put a contract except
-- back in the branch.
--
-- SHADOW IS NOT RETRACTION. A retraction (location_claim_retractions, section
-- 4.6) is a permanent, reasoned, append-only statement that named claims are
-- WRONG. Shadow is a reversible statement that a contract is UNPROVEN: its claims
-- are still mined, still stored, still auditable against the frozen sample - they
-- simply are not resolver inputs yet. Un-shadowing rewrites no claim, because the
-- claims were being written the whole time.
--
-- WHY THE VIEW AND NOT THE RESOLVER. 01 section A.2 check 9: section 03 never
-- selects from location_claims directly, only from location_claims_live - which
-- is exactly why a retraction cannot be silently ignored. Shadow rides the same
-- rail: enforcing it in the view means every present and future resolver read
-- inherits it, and there is no code path that can forget to ask.
--
-- The flag lives on the HEADER (portal_contracts), not the entry, for the same
-- reason is_active does: the frozen sample scores a CONTRACT VERSION, and a
-- half-shadowed contract would be a mixture nothing can gate on. It is MUTABLE
-- like is_active - the YAML sets the value a fresh projection lands with, and
-- clearing it afterwards is an operational UPDATE, not a contract_version bump
-- (the extraction did not change; the evidence did). contracts.py hashes the
-- YAML with its top-level `shadow:` line removed, so flipping the flag in git
-- never trips the contract_sha256 immutability check either.
--
-- THREE RELATIONS, ONE PREDICATE EACH. The retraction predicate now lives in
-- exactly one place, location_claims_unretracted, and its two children partition
-- it: location_claims_live (what the resolver reads) and location_claims_shadow
-- (what the SCORER reads). Stating the correlated retraction predicate a second
-- and third time was the alternative, and a drifted copy is silent.
--
-- WHY A SCORER RELATION AT ALL. Shadow is only a gate if the operator can measure
-- what the dark contract WOULD produce; with no read path the flag is a one-way
-- door and "un-shadow once its sample passes" is unsatisfiable. The frozen-sample
-- scorecard reads location_claims_shadow directly
-- (toolkit/location_labels.score_shadow_claims) and scores the CONTRACT's own
-- assertions - street/obec/okres - against the same labels and the same floors as
-- the live projection. The fourth floor, precision class, is a resolver-derived
-- granularity and is therefore measured by the normal scorecard AFTER un-shadowing;
-- that is safe because un-shadowing is reversible and re-queues the same listings
-- either way (see below).
--
-- WHY A NEW dirty_locations REASON. Flipping the flag changes what the resolver
-- may consume for every listing the contract has ever claimed, and nothing else
-- would ever re-resolve them: the claim rows are untouched (claims_intake enqueues
-- only NEWLY INSERTED claims) and the daily backstop re-queues only a missing
-- projection or a stale version tuple (resolver/drain._FULL_SWEEP_SQL), neither of
-- which a flag flip moves. So contracts.set_shadow enqueues them itself, in the
-- same transaction as the UPDATE, and the queue says why.
--
-- ABSENCES ARE DELIBERATELY NOT FILTERED. location_claim_absences (382) records
-- "this contract looked and found nothing"; a shadowed contract still writes them.
-- That is inert today - nothing in the resolver reads that table - and it cannot
-- be fixed with a predicate anyway: the table carries no contract_entry_id, only
-- an extractor_version text ('contract:X@2'), so excluding shadowed absences is a
-- MIGRATION (add the FK) in W2-2/W2-13, not a WHERE clause here.
--
-- Additive only: one nullable-free defaulted boolean, one widened CHECK, one new
-- index, two new views, and a CREATE OR REPLACE of one view whose column list is
-- unchanged.

begin;

------------------------------------------------------------------
-- portal_contracts.shadow
------------------------------------------------------------------

alter table portal_contracts
  add column if not exists shadow boolean not null default false;

comment on column portal_contracts.shadow is
  'W2 gate state (06 section 6.4.0(2)): true = claims are mined and stored but '
  'excluded from location_claims_live (and exposed to the scorer through '
  'location_claims_shadow), so the resolver never consumes them. Cleared by the '
  'operator once the frozen labelled sample for this contract version meets its '
  'precision floors; rewrites no claim, because the claims were on disk the whole '
  'time.';

------------------------------------------------------------------
-- The flip has to find the contract's claims. location_claims has no index on
-- contract_entry_id (382 indexes listing, fingerprint, snapshot, payload, geom,
-- source, value_norm and extraction_method - not this), so the enqueue would be a
-- sequential scan of the whole claim spine. Plain CREATE INDEX, not CONCURRENTLY:
-- this file is applied as one transaction and CONCURRENTLY cannot run inside one.
-- It takes a SHARE lock, so apply it when the detail drain can afford to wait
-- (the intake retries; see location_data.resolver drain resilience).
------------------------------------------------------------------

create index if not exists location_claims_contract_entry
  on location_claims (contract_entry_id)
  where contract_entry_id is not null;

------------------------------------------------------------------
-- dirty_locations.reason - 'contract_shadow'
--
-- The CHECK is 384's inline column constraint, so the name is Postgres-generated
-- and deterministic (dirty_locations_reason_check). Dropped by that name WITHOUT
-- `if exists`: a rename would otherwise leave the old constraint in place next to
-- the new one, silently rejecting the very value this migration adds.
------------------------------------------------------------------

alter table dirty_locations drop constraint dirty_locations_reason_check;
alter table dirty_locations add constraint dirty_locations_reason_check
  check (reason in
    ('claim_insert', 'resolution_written', 'registry_version', 'policy_version',
     'collision_recompute', 'property_grouping', 'operator_edit', 'full_sweep',
     'contract_shadow'));

------------------------------------------------------------------
-- location_claims_unretracted - the retraction predicate, stated ONCE.
--
-- The body below is migration 382's location_claims_live verbatim. It is not a
-- resolver relation and nothing may read it directly; it exists so that the two
-- relations that ARE read cannot drift apart on what "retracted" means.
------------------------------------------------------------------

create or replace view location_claims_unretracted as
  select c.*
  from location_claims c
  where not exists (
    select 1 from location_claim_retractions r
    where (r.scope = 'claim'            and r.claim_id = c.id)
       or (r.scope = 'batch'            and r.batch_id = c.batch_id)
       or (r.scope = 'extractor_entry'  and r.contract_source = c.source
                                        and r.extractor_id   = c.extractor_id
                                        and r.contract_version is not distinct from
                                            (select pc.version from portal_contract_entries pce
                                               join portal_contracts pc on pc.id = pce.contract_id
                                              where pce.id = c.contract_entry_id))
       or (r.scope = 'contract_version' and r.contract_source = c.source
                                        and r.contract_version is not distinct from
                                            (select pc.version from portal_contract_entries pce
                                               join portal_contracts pc on pc.id = pce.contract_id
                                              where pce.id = c.contract_entry_id)));

------------------------------------------------------------------
-- location_claims_live - unretracted MINUS shadowed.
--
-- Still `select <all of location_claims>` in the same column order, so this is a
-- legal CREATE OR REPLACE of 382's view.
--
-- contract_entry_id is NULLABLE (a legacy-column or operator claim has no
-- contract entry). NOT EXISTS gives those rows the right answer for free: no
-- joined row means nothing shadows them, so they stay live - which is what we
-- want, since shadow is a statement about a CONTRACT and they have none.
------------------------------------------------------------------

create or replace view location_claims_live as
  select u.*
  from location_claims_unretracted u
  where not exists (
    select 1 from portal_contract_entries pce
      join portal_contracts pc on pc.id = pce.contract_id
     where pce.id = u.contract_entry_id and pc.shadow);

------------------------------------------------------------------
-- location_claims_shadow - unretracted AND shadowed: the exact complement.
--
-- EXISTS, not `NOT NOT EXISTS`: a claim with no contract entry is in neither
-- relation's shadow half, which is the same answer both views give it.
------------------------------------------------------------------

create view location_claims_shadow as
  select u.*
  from location_claims_unretracted u
  where exists (
    select 1 from portal_contract_entries pce
      join portal_contracts pc on pc.id = pce.contract_id
     where pce.id = u.contract_entry_id and pc.shadow);

comment on view location_claims_shadow is
  'W2 (06 section 6.4.0(2)): claims that are stored and unretracted but excluded '
  'from location_claims_live because their contract version is shadowed. A '
  'SCORING surface only - toolkit/location_labels.score_shadow_claims measures '
  'these against the frozen labelled sample so the un-shadow gate is decidable. '
  'The resolver must never read it (01 section A.2 check 9); a test asserts that.';

-- CREATE OR REPLACE VIEW preserves the object's ACL, so the 382 revoke still
-- stands for location_claims_live. Restated because this project auto-GRANTs the
-- browser roles on new objects and a future replace that DROPs first would
-- silently re-open it; the two new views need it outright.
revoke all on location_claims_live        from anon, authenticated;
revoke all on location_claims_unretracted from anon, authenticated;
revoke all on location_claims_shadow      from anon, authenticated;

commit;
