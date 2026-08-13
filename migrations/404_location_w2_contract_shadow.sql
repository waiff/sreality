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
-- simply are not resolver inputs yet. Un-shadowing is one UPDATE and needs no
-- backfill, because the claims were being written the whole time.
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
-- (the extraction did not change; the evidence did).
--
-- Additive only: one nullable-free defaulted boolean on a table that is a
-- deploy-time projection of git, and a CREATE OR REPLACE of one view whose
-- column list is unchanged.

begin;

------------------------------------------------------------------
-- portal_contracts.shadow
------------------------------------------------------------------

alter table portal_contracts
  add column if not exists shadow boolean not null default false;

comment on column portal_contracts.shadow is
  'W2 gate state (06 section 6.4.0(2)): true = claims are mined and stored but '
  'excluded from location_claims_live, so the resolver never consumes them. '
  'Cleared by the operator once the frozen labelled sample for this contract '
  'version meets its precision floors; needs no backfill, because the claims '
  'were on disk the whole time.';

------------------------------------------------------------------
-- location_claims_live - now TWO exclusions, composed.
--
-- The retraction predicate is carried over from migration 382 verbatim; the
-- shadow predicate is appended. The column list is `c.*` in both, so this is a
-- legal CREATE OR REPLACE.
--
-- contract_entry_id is NULLABLE (a legacy-column or operator claim has no
-- contract entry). NOT EXISTS gives those rows the right answer for free: no
-- joined row means nothing shadows them, so they stay live - which is what we
-- want, since shadow is a statement about a CONTRACT and they have none.
------------------------------------------------------------------

create or replace view location_claims_live as
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
                                              where pce.id = c.contract_entry_id)))
    and not exists (
    select 1 from portal_contract_entries pce
      join portal_contracts pc on pc.id = pce.contract_id
     where pce.id = c.contract_entry_id and pc.shadow);

-- CREATE OR REPLACE VIEW preserves the object's ACL, so the 382 revoke still
-- stands. Restated because this project auto-GRANTs the browser roles on new
-- objects and a future replace that DROPs first would silently re-open it.
revoke all on location_claims_live from anon, authenticated;

commit;
