-- 519_rollback_w9_contract_activation.sql
--
-- ROLLBACK INSURANCE, NOT A SCHEMA CHANGE. Declares no relation, column, index, grant or
-- policy (`scripts.apply_migration_receipt --plan` reports zero probeable objects, which
-- is the point: there is nothing new to verify afterwards). It flips
-- `portal_contracts.is_active` on eight portals from the version PR #1466 (W9) introduced
-- back to the version before it, so the claims ALREADY STORED under the previous version
-- become visible to the resolver again.
--
-- WHY THIS WORKS. `location_data/resolver/resolve_db.py` admits a portal claim only while
-- its entry's contract header is active:
--
--     EXISTS (SELECT 1 FROM portal_contract_entries pce
--               JOIN portal_contracts pc ON pc.id = pce.contract_id
--              WHERE pce.id = c.contract_entry_id AND pc.is_active)
--
-- W9 bumped eight contracts (their entries stopped chaining the deleted
-- `statutory_city_obec` transform), which retired the previous versions. The corpus
-- re-resolve then ran BEFORE the re-mine had refilled `location_claims` under the new
-- versions, so ~596k listings were judged against no live claims at all. Re-activating
-- the previous version restores that evidence without moving a single claim row: nothing
-- was deleted (W9 bumped, it did not call `contracts.retract()`), the rows were only
-- hidden behind `is_active`.
--
-- THIS MIGRATION IS THE SMALLER HALF OF THE ROLLBACK. The other half is in the same PR
-- and is what makes the flip STICK: `contracts/portals/*.yaml` are restored to their
-- pre-W9 BYTES, so the intake lane's "Project the portal contracts from git" step
-- (`python -m location_data.contracts --load`, the first step of every
-- `location_claims_intake.yml` run) re-asserts the SAME version, with the same governed
-- sha256 already on record — no insert, no immutability refusal. Without that restore
-- `project(activate=True)` — which deactivates every other header for the source and then
-- activates whatever version the YAML names, in that order, whatever the numbers are —
-- would undo this migration on the next intake run, within the hour.
--
-- MERGE FIRST, THEN APPLY. Applied against a deployment still serving the W9 YAML this
-- buys at most one intake cycle. Applied after the merge it is purely an accelerator: it
-- does immediately what the next projection would do anyway.
--
-- IDEMPOTENT. Re-running is a no-op: the target row is already active and the W9 row is
-- already retired.
--
-- NOT TOUCHED: `mmreality` (W9 did not bump it, it stays at @3);
-- `portal_contract_entries` (immutable, append-only); `location_claims` (nothing is
-- deleted here — the claims mined under the W9 versions stay on disk and come back the
-- moment W11 or a later bump re-activates their header).
--
-- AFTERWARDS the corpus must be re-resolved so the restored claims reach the read model:
--     gh workflow run location_resolve.yml --ref main -f mode=full-resolve

-- One DO block, deliberately: the eight (source, pre_w9_version) pairs are stated once,
-- and a temp table would be reported as a "declared object" by the apply workflow's
-- receipt probe and then fail it (a temp table is gone by the time the probe runs).
do $$
declare
  r            record;
  active_now   int;
  n_entries    bigint;
  deactivated  int := 0;
  activated    int := 0;
begin
  for r in
    -- The eight contracts W9 bumped. `mmreality` is absent deliberately: it carried no
    -- `statutory_city_obec` entry and was never bumped.
    select * from (values
      ('sreality',     3, 4),
      ('idnes',        4, 5),
      ('bazos',        5, 6),
      ('maxima',       3, 4),
      ('realitymix',   5, 6),
      ('ceskereality', 6, 7),
      ('bezrealitky',  2, 3),
      ('remax',        4, 5)
    ) as t(source, pre_w9_version, w9_version)
  loop
    -- GUARD 1: the version being rolled BACK TO must still be on record with entries. A
    -- missing header (or one whose entries went) means that version was retracted --
    -- `contracts.retract()` DELETES its claims -- and re-activating it would then serve
    -- an EMPTY evidence set: worse than the state this migration is fixing, and silent.
    select count(*) into n_entries
      from portal_contract_entries pce
      join portal_contracts pc on pc.id = pce.contract_id
     where pc.source = r.source and pc.version = r.pre_w9_version;
    if n_entries = 0 then
      raise exception
        'rollback refused: %@% has no portal_contract_entries rows -- the pre-W9 version '
        'is not on record (retracted?), so re-activating it would serve no claims at all',
        r.source, r.pre_w9_version;
    end if;

    -- Order matters: `portal_contracts_active` (mig 382) is a NON-deferrable unique index
    -- on (source) WHERE is_active, so the incumbent is stood down before the survivor
    -- comes up. This mirrors `contracts.project()`'s own _DEACTIVATE_SQL/_ACTIVATE_SQL.
    update portal_contracts
       set is_active = false, retired_at = coalesce(retired_at, now())
     where source = r.source and is_active and version <> r.pre_w9_version;
    get diagnostics active_now = row_count;
    deactivated := deactivated + active_now;

    update portal_contracts
       set is_active = true, retired_at = null
     where source = r.source and version = r.pre_w9_version and not is_active;
    get diagnostics active_now = row_count;
    activated := activated + active_now;

    -- GUARD 2: land it or fail. The unique index already guarantees "at most one active
    -- header per source"; this asserts "exactly one, and it is the pre-W9 one".
    select count(*) into active_now
      from portal_contracts
     where source = r.source and is_active and version = r.pre_w9_version;
    if active_now <> 1 then
      raise exception 'rollback did not land for %: % active header(s) at version %',
        r.source, active_now, r.pre_w9_version;
    end if;
  end loop;

  raise notice
    'W9 rollback: 8 portals pinned to their pre-W9 contract version (% retired, % re-activated)',
    deactivated, activated;
end $$;
