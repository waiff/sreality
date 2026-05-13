-- 037_estimation_condition_scenarios.sql
--
-- Adds condition-scenario fan-out to estimation_runs. The user-facing
-- problem: a single estimate against today's "as-is" condition is
-- often the least useful number a non-technical owner can get — they
-- want to know what the unit would rent for / sell for if renovated,
-- and what it's worth right now in its current state. Per
-- agreed plan (see ROADMAP "Phase U2.9 — condition scenarios"):
--
--   * The agent (rental_estimator_full_v1, sales_estimator_*) runs
--     the as-is estimation as it does today. That parent run is
--     unchanged from the caller's perspective — same row, same
--     range columns, same trace.
--   * Where comparable supply allows (≥ N matches per condition
--     bucket, today N=30), the agent fans out one additional
--     child estimation_runs row per alternative condition bucket.
--     Each child has its own input_spec (a copy with the synthetic
--     condition filter applied), its own comparables_used, its own
--     trace fragment, its own rent/price range. Children link to
--     the parent via condition_scenario_parent_id below.
--   * Where comparable supply is too thin to support a per-bucket
--     cohort (e.g. apartment rentals where 'před rekonstrukcí'
--     nationally has ~33 listings), the agent computes a benchmark
--     haircut from a wider cohort and persists the derived range on
--     a child row with basis='benchmark'. The detail page surfaces
--     this distinction so operators don't conflate the two paths.
--
-- Why a new FK column instead of reusing parent_run_id or
-- building_run_id:
--   * parent_run_id (migration 010) means "this run is a re-run of
--     that one — please supersede in any UI listing". Scenarios are
--     siblings, not replacements; conflating them would break the
--     re-run UX.
--   * building_run_id (migration 035) means "this run is one apartment
--     unit inside that pasted whole-building listing". A building
--     child can independently produce its own condition scenarios,
--     so the two linkages are orthogonal and must coexist.
--
-- All four new columns are nullable. A parent (as-is) row has
-- condition_scenario_parent_id NULL and condition_scenario_kind set
-- to 'as_is' (or NULL on historical rows — readers default to 'as_is'
-- when NULL on a row with no parent). A child row has both set.
--
-- The bucket taxonomy (condition_scenario_kind) is intentionally
-- coarser than the raw `listings.condition` values (11 distinct
-- strings in production). The mapping from raw → bucket lives in
-- application code (api/condition_buckets.py) so the skill prompt
-- and the API can share it without a JOIN. CHECK constraint enforced
-- here; adding a new bucket is a one-line ALTER.
--
-- condition_scenario_basis distinguishes the two production paths:
--   * 'comparables' — child has its own per-bucket cohort.
--     comparables_used populated normally.
--   * 'benchmark'  — child's range derived from applying a haircut
--     (Δ% pulled from a wider cohort) to the parent's range. The
--     derivation details (cohort filters used, Δ% applied, sample
--     size of the wider cohort) live in `comparables_used` under a
--     `benchmark` key so they're auditable but don't need their
--     own column.
--
-- condition_scenario_label is the agent-authored short label shown
-- in the UI ("Po rekonstrukci", "Před rekonstrukcí", "Stav jako
-- nyní (k bydlení)", etc.). The bucket kind is the stable machine
-- label; the label is the human-readable one. Free text.

alter table estimation_runs
  add column condition_scenario_parent_id bigint
    references estimation_runs(id) on delete set null,
  add column condition_scenario_kind text
    check (condition_scenario_kind is null or condition_scenario_kind in (
      'as_is', 'renovated', 'unrenovated', 'mid', 'new_build', 'custom'
    )),
  add column condition_scenario_basis text
    check (condition_scenario_basis is null or condition_scenario_basis in (
      'comparables', 'benchmark'
    )),
  add column condition_scenario_label text;

create index estimation_runs_cond_scenario_parent_idx
  on estimation_runs (condition_scenario_parent_id)
  where condition_scenario_parent_id is not null;
