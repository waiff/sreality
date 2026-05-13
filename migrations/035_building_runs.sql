-- 035_building_runs.sql
--
-- (Originally drafted as 034; renumbered to 035 because slot 034 was
-- claimed by 034_estimation_runs_provider_skill.sql on
-- claude/review-roadmap-scope-BHt5w, which was applied to the live
-- DB before this migration. Renumbered before apply; safe coexist —
-- the two migrations touch disjoint columns on estimation_runs.)
--
-- Parent grouping for the "paste a whole-building listing → decompose
-- into apartment units → estimate each → roll up + business case"
-- workflow. One row per pasted house listing. Per CLAUDE.md
-- architectural rule #13.
--
-- Status lifecycle:
--   pending          — row created, nothing parsed/extracted yet.
--   extracting       — agent is reading description + images (B1).
--   awaiting_input   — unit proposal ready; UI waits for the operator
--                      to confirm / edit / approve the unit list.
--   estimating       — per-unit child estimations are fanning out (B2).
--   success          — all children finished, totals rolled up.
--   failed           — parse, extraction, or all children failed.
--
-- The awaiting_input state is the human-in-the-loop pause that
-- distinguishes the building flow from today's single-shot
-- estimation_runs flow. estimation_runs.status itself is unchanged:
-- the children each run the normal pending/running/success/failed
-- lifecycle, oblivious to the parent's pause.
--
-- Why a separate parent table rather than extending estimation_runs:
--   * estimation_runs is one row per single estimate. A building has
--     N children plus rollup totals plus an operator-confirmed unit
--     list plus a business case — qualitatively different state.
--   * `parent_run_id` on estimation_runs already means "re-run of
--     that run". Overloading it to also mean "child unit of that
--     building" would confuse every existing reader.
--
-- Units (operator-curated, ~5-10 entries) live as JSONB on the
-- parent, not as a separate normalised table. Per CLAUDE.md
-- conventions: a separate table would only earn its keep if units
-- became richly queryable on their own; for v1 they're confirmed
-- inputs to the fan-out, not analytical objects.
--
-- `units_proposal` holds the agent's tentative extraction output
-- (append-only after the extractor runs in B1). `units` holds the
-- operator-confirmed list, mutable until estimation starts. Keeping
-- them separate preserves the extractor's original guess for audit
-- alongside the operator's edits.
--
-- Per-unit child estimations point back through two new
-- `estimation_runs` columns:
--   building_run_id   — FK to this table; ON DELETE SET NULL so
--                       parent removal doesn't drop the historical
--                       child estimates. Mirrors `parent_run_id`'s
--                       semantics in migration 010.
--   building_unit_id  — text id (e.g. "u1") matching an entry in
--                       the parent's `units` JSONB array. No FK —
--                       JSONB arrays can't enforce one cheaply, and
--                       the parent never re-ids units after
--                       estimation starts (B2 contract).
--
-- The `business_case` column is reserved for Phase B3. JSONB grain
-- because the spreadsheet is non-tabular and operator-tunable. Math
-- engine lives in api/business_case.py (B3); the column is the
-- persisted input + cached output.
--
-- RLS enabled; NO policies. The frontend reads buildings through
-- the API, not direct Supabase anon — same pattern as
-- estimation_runs (migration 010).

create table building_runs (
  id                          bigserial primary key,
  created_at                  timestamptz not null default now(),

  source                      text not null
    check (source in ('ui', 'api', 'clickup')),
  status                      text not null
    check (status in (
      'pending', 'extracting', 'awaiting_input',
      'estimating', 'success', 'failed'
    )),

  input_url                   text,
  input_sreality_id           bigint,
  input_spec                  jsonb,

  source_kind                 text,
  parse_confidence            text,
  parse_confidence_per_field  jsonb,
  source_html                 text,

  subject_summary             jsonb,

  units_proposal              jsonb,
  units                       jsonb,

  total_rent_p25_czk          integer,
  total_rent_p50_czk          integer,
  total_rent_p75_czk          integer,
  total_sale_p25_czk          bigint,
  total_sale_p50_czk          bigint,
  total_sale_p75_czk          bigint,

  business_case               jsonb,

  warnings                    jsonb,
  error_message               text
);

create index on building_runs (created_at desc);
create index on building_runs (status);
create index on building_runs (input_sreality_id);

alter table building_runs enable row level security;

alter table estimation_runs
  add column building_run_id  bigint
    references building_runs(id) on delete set null,
  add column building_unit_id text;

create index on estimation_runs (building_run_id);
