-- 502_location_w2b_drop_old_projection.sql
--
-- Location simplification sprint, wave W2-b. **DESTRUCTIVE — APPLY ONLY AFTER
-- THE GATE BELOW IS GREEN.** Rule 25 ("one store, one lane, eleven claim types,
-- no flags; every location PR deletes at least as much as it adds"): W2-a built
-- the one answer table (`listing_location`, migration 501) and stopped writing
-- everything below. This file removes what is left standing.
--
-- THE GATE. Merge the PR, let Railway go green, then let the worker lane
-- re-resolve the corpus under the bumped RESOLVER_VERSION and check BOTH:
--
--   1. count(listing_location) = count(listings where is_active)
--      — rule 25's coverage invariant, measured by
--        verify_pipeline's `location_town_coverage` check.
--   2. the per-portal `cz_no_town` line is back at or below its pre-W2 level
--      — the same check's second arm. A portal that regressed means BIND is
--        losing towns the old projection had, and that is a resolver bug to fix
--        BEFORE the old rows are unavailable to compare against.
--
-- Until this file is applied the code in the same PR runs fine: every reader it
-- cuts over reads `listing_location`, which exists since 501, and nothing in the
-- tree names a relation dropped below (proved by the CI SQL-correctness sweep,
-- `tests/test_sql_schema_prepare.py`, which PREPAREs every runtime statement
-- against the replayed schema — this file included).
--
-- WHAT THIS IS, IN ONE LINE: rebuildable caches and observability, not history.
-- Nothing below is a store of record. The projections are a pure function of
-- `location_claims` + the RÚIAN mirror (`dirty_locations` → drain rebuilds any
-- row on demand); the resolution/candidate/contradiction tables are the trace of
-- an engine that no longer exists; the policy tables are code now
-- (`grade.py`'s per-level dict, `fill.py`'s registry-first rule); the compare
-- cohort and the labelled samples are derived review surfaces. The operator gate
-- for a destructive migration (database skill) is the confirmation above plus a
-- `pg_dump` of the schema before the apply — take one, because "rebuildable"
-- means rebuildable by a resolver, not recoverable from a backup nobody made.
--
-- STATEMENT AUTOCOMMIT, NOT ONE TRANSACTION. No `begin;`/`commit;`: the apply
-- path is `psql -f`, which runs each statement in its own implicit transaction
-- (`apply_migration.yml`, and CI's replay uses the same invocation). 498's
-- single-transaction form deadlocked against the continuous location lanes — one
-- long transaction holding ACCESS EXCLUSIVE on twenty relations is a lock queue
-- every reader parks behind. Every statement here is `if exists` or otherwise
-- guarded, so the workflow's lock-timeout retry (which re-runs the WHOLE file)
-- is free, and a partial apply is simply resumed by running it again.
--
-- lock_timeout is long for the same reason 498's was: the location lanes hold
-- short locks constantly, and a 5 s budget spends thirty retries losing every
-- race. Nothing dropped below is written by anything any more, and the one
-- periodic READER — the compare-cohort cron — is unscheduled in section 1,
-- before its tables are touched.

set lock_timeout = '600s';

------------------------------------------------------------------
-- 1. The pg_cron job FIRST.
--
--    `refresh_location_compare_cohort()` (migration 493) runs at 11,41 * * * *
--    and reads eighteen `listing_location_current` columns plus six
--    `property_location_current` ones. Dropping those tables under a live
--    schedule does not raise here — it breaks the job SILENTLY, on a tick that
--    happens after this file has exited 0, and the only trace is a row in
--    cron.job_run_details nobody reads.
--
--    Unscheduled by JOB ID looked up from the name, because cron.unschedule(name)
--    raises when the job is absent while the id form simply selects no rows. The
--    whole block is wrapped: pg_cron's `cron` schema does not exist in the CI
--    replay Postgres, and a missing schema is 3F000, not a no-op.
------------------------------------------------------------------

do $cron$
begin
  perform cron.unschedule(jobid)
  from cron.job
  where jobname = 'location-compare-cohort-refresh'
     or command like '%refresh_location_compare_cohort%';
exception when others then
  raise notice 'pg_cron not present or job already gone (%); nothing unscheduled', sqlerrm;
end
$cron$;

-- Corollary E's registry row goes with its artifact: a `derived_artifacts` row
-- whose producer no longer exists would read as an artifact permanently past its
-- staleness budget, which is the alarm, not the fix.
delete from public.derived_artifacts where name = 'location_compare_cohort';

------------------------------------------------------------------
-- 2. The compare cohort (migration 493) — function, then relations.
--
--    `location_compare_cohort_next` is the blue-green build target the refresh
--    creates and swaps; it exists between the CTAS and the rename, so a cancelled
--    tick can leave one behind.
------------------------------------------------------------------

drop function if exists refresh_location_compare_cohort();

drop table if exists location_compare_cohort_next;
drop table if exists location_compare_cohort;
drop table if exists location_compare_cohort_state;

------------------------------------------------------------------
-- 3. The labelled samples (migration 399).
--
--    Frozen operator ground-truth over a 200-row draw per portal, scored against
--    the projection by `toolkit/location_labels.py`. The scoring module, its four
--    API routes and the SPA section go in the same PR: the sample's whole purpose
--    was old-vs-new precision during W1v, and "new" is now the only system.
--    The draw script it names has not existed for waves.
------------------------------------------------------------------

drop table if exists location_labelled_sample_members;
drop table if exists location_labelled_samples;

------------------------------------------------------------------
-- 4. The contradiction ledger (migration 384).
--
--    Nine reconciler rules, an auto-close engine and a disposition log, replaced
--    by `listing_location.disputed` — one nullable text column whose value IS the
--    reason. The view first: it selects from the table under it.
------------------------------------------------------------------

drop view if exists location_contradictions_open;

drop table if exists location_contradiction_disposition_log;
drop table if exists location_contradiction_dispositions;
drop table if exists location_contradictions;

------------------------------------------------------------------
-- 5. The two projections (migration 384, + 388's quality columns).
--
--    Dropped BEFORE `location_resolutions` and `pin_cluster_epochs`: both carry
--    an inline REFERENCES to them, and dropping a parent first would need CASCADE,
--    which is exactly the blunt instrument that takes something unintended with it.
--    Every FK in this section is internal to the set being dropped, so no CASCADE
--    is needed anywhere in this file.
--
--    `listing_location_current` is 81 columns over ~756k rows;
--    `property_location_current` is 36 and was a verbatim copy of its winner's
--    row (493's own header measured `p.kraj_kod` and `w.kraj_kod` agreeing on
--    0 of 637,381 rows).
------------------------------------------------------------------

drop table if exists listing_location_current;
drop table if exists property_location_current;

------------------------------------------------------------------
-- 6. The resolution trace (migration 383).
--
--    One row per (listing, claim-set, resolver, registry, policy, epoch) tuple,
--    with the full candidate ladder beside it and a verification table that never
--    got a writer. W2-a's resolver stores no trace: a row REPLAYS from the three
--    version ids stamped on `listing_location`, which is the property the AST
--    purity scan exists to keep true.
--
--    THE FK IS CIRCULAR, so the constraint comes off first. `candidates.resolution_id`
--    references `location_resolutions(id)` and `location_resolutions.chosen_candidate_id`
--    references `location_resolution_candidates(id)` back (383:167, DEFERRABLE) — neither
--    table can be dropped before the other. The way out is either CASCADE or this one
--    named constraint; the constraint is the narrow instrument, and CASCADE is the one
--    that silently takes whatever else happens to depend on the table. CI's replay found
--    this, which is the argument for using no CASCADE anywhere in this file: the failure
--    was loud and local.
------------------------------------------------------------------

alter table if exists location_resolutions
  drop constraint if exists location_resolutions_chosen_fk;

drop table if exists location_resolution_verifications;
drop table if exists location_resolution_candidates;
drop table if exists location_resolutions;

------------------------------------------------------------------
-- 7. The pin-collision epoch (migrations 383 + 384).
--
--    Unscheduled since 2026-08-11; its producer was deleted in W2-a. It fed
--    `pin_collision_class`, a precision axis the answer table does not carry —
--    the shared-pin count is a read-time aggregate in W3's browse_list rebuild.
------------------------------------------------------------------

drop table if exists pin_cluster_daily_summary;
drop table if exists pin_clusters;
drop table if exists pin_cluster_epochs;

------------------------------------------------------------------
-- 8. The config tables that became code (migrations 380 + 383).
--
--    location_field_policy      -> survivorship is `fill.py`'s registry-first rule
--    location_uncertainty_policy-> `grade.py`'s per-level constant dict, carrying
--                                  383's + 491's own v1 numbers verbatim
--    location_collision_policy  -> deleted with the collision engine
--    location_constants         -> the CZ bbox and the 250 m sliver tolerance are
--                                  module constants in `claims_common` / `bind`
--    location_level_granularity -> the ruian_level → granularity map is the BIND
--                                  rung's own output now
--    location_metrics_rollup    -> never had a producer
--
--    `location_granularity_rank` deliberately SURVIVES: it is the ordinal source
--    every consumer compares by (`toolkit/dedup_candidates_sql.py` joins it in
--    seven statements) and rank-by-lookup is the whole reason enum ordinality is
--    banned from persisted artefacts.
------------------------------------------------------------------

drop table if exists location_field_policy;
drop table if exists location_uncertainty_policy;
drop table if exists location_collision_policy;
drop table if exists location_constants;
drop table if exists location_level_granularity;
drop table if exists location_metrics_rollup;

------------------------------------------------------------------
-- 9. The block-key helpers (migration 384).
--
--    Four IMMUTABLE functions that minted the projection's stored blocking keys
--    (`geo_cell_key`, `street_block_key`, `addr_block_key`,
--    `building_block_key`). Their Python twin (`resolver/derived.py`) went in
--    W2-a and the columns go in section 5; a stored derivation is a second
--    definition, so W3 derives what it needs at read.
------------------------------------------------------------------

drop function if exists location_geo_cell_key(geometry);
drop function if exists location_street_block_key(bigint, text, text);
drop function if exists location_addr_block_key(bigint);
drop function if exists location_building_block_key(bigint);

------------------------------------------------------------------
-- 10. The enum types left with no column.
--
--     Dropped WITHOUT cascade on purpose: if anything still types a column with
--     one of these, this raises 2BP01 and the apply stops here rather than taking
--     that column's table with it. Checked against the whole migration history —
--     these four are named only by relations dropped above, or by relations 498
--     already dropped (`radius_semantics` typed `location_claim_links`, and 382's
--     own header records that `location_claims` never carried the column).
--
--     NOT dropped, and each for a reason a later session will want:
--       position_source  — `portal_contract_entries.default_position_source`
--       blur_evidence    — `location_claims.blur_evidence` and the same table's
--                          `default_blur_evidence`
--       location_granularity / match_confidence / country_status — the answer
--                          table's three grade-and-status columns.
--
--     `pin_collision_class` is NOT here because it was never a type: 384 spells
--     it as `text not null default 'normal' check (... in (six values))`, carried
--     verbatim from `pin_clusters.classification`. It goes with its columns.
------------------------------------------------------------------

drop type if exists resolution_status;
drop type if exists country_determination_method;
drop type if exists admin_assignment_method;
drop type if exists radius_semantics;

------------------------------------------------------------------
-- 11. NOT DONE HERE: deleting the superseded claims.
--
--     `_CLAIMS_SELECT` admits a claim only when its `contract_entry_id` belongs
--     to an ACTIVE contract header (plus operator claims, which carry no entry),
--     so W1-c's nine simultaneous contract bumps left every prior version's rows
--     on disk, filtered at read. Deleting them is hygiene, not correctness — and
--     it does not belong in this file:
--
--     "the contract's claims" is every listing the portal has ever had (~5 M rows
--     on sreality alone). A `do $$ ... $$` loop cannot COMMIT, so batching inside
--     one would still be ONE transaction: it would hold locks for its whole run
--     and, if it were killed, make no progress at all — the exact failure mode
--     `contracts.retract()` was rewritten in bounded, individually-committed
--     batches to avoid.
--
--     So the cleanup is the tool that already does it correctly, run per inactive
--     contract version once this file is applied:
--
--       python -m location_data.contracts --retract <portal>@<version>
--
--     Interrupted, its committed batches are real and a re-run resumes. It
--     enqueues each touched listing into `dirty_locations`, so the drain
--     re-resolves from what survives — which for an inactive version's rows is a
--     no-op, since the resolver was not reading them anyway.
------------------------------------------------------------------
