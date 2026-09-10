-- 492: NEW DEDUP Wave 2 — the Level-0 candidate store (evidence tier).
-- Design: docs/design/new-dedup/PROGRAM.md ("Simulation architecture": "candidate pairs
-- from L0, keyed by listing pair + path + inputs"; ledger 2026-09-10 (a) for the path C
-- ruling this store is built for first).
--
-- WHAT A CANDIDATE IS. Level 0 does not decide anything — it only says which two listings
-- are worth comparing at the later levels (pHash, embeddings, vision). A row here is one
-- such pair, found by one PATH (the way the pair was looked for) on one RUNG (which
-- attributes were available to compare), under one set of INPUTS (the operator's settings
-- at the time). Path C, the first one built, replaces every "street + geo + N metres" test
-- of path A with "same town" (the location engine's listing-grain projection, its `obec_kod`
-- column — docs/design/location-serving-contract.md), because the location
-- data on input is only reliably right at town grain; its rungs are C1 (town + disposition)
-- and C3 (town + area, taken when a disposition is missing on either side).
--
-- WHY THREE TABLES. "One row per listing pair × path × the inputs that produced it, with a
-- fingerprint so a changed input re-generates the pair" (operator brief, 2026-09-10) needs
-- the inputs to be part of a pair's identity. Spelling (path, fingerprint) into every one of
-- what will be tens of millions of rows would fatten the primary key with two text columns;
-- so the parameter set is a row of its own (`candidate_inputs`) and pairs carry its
-- surrogate. A GENERATION is one run of one parameter set: re-running the same inputs
-- UPSERTS into the same pair rows (`generation_id` and `last_seen_at` move; nothing is
-- duplicated), and running changed inputs writes a disjoint set of rows under a new
-- inputs_id — the old rows stay until pruned, so two parameter sets can be compared.
--
-- WHY THE PAIR ROW IS NARROW AND TYPED (no per-row jsonb). The scouting sample of
-- 2026-09-10 put path C at roughly 10^8 pairs all-time for Praha alone (one rent/byt/2+kk
-- bucket ≈ 16k listings). Every byte per row is a gigabyte per store. The evidence a later
-- level or the audit page needs — which disposition matched, the two areas and their gap,
-- the two floors and whether the floor rule could be checked at all — is a handful of
-- typed columns; path A will ADD its own (distance, street key) additively, per the design's
-- "additive columns expected as criteria evolve".
--
-- LISTING GRAIN, `listings.id`. The pair is (lo, hi) with lo < hi enforced, so one real pair
-- has exactly one row. Legacy merge decisions are ignored by design (two listings already
-- under one property are still a pair — that is how the simulation is later checked against
-- them), and NO foreign key points at `listings`: the sim schema is dropped wholesale at
-- Wave 8 and must never hold a production table hostage.
--
-- `simulation_runs.triggered_by` gains 'lane': the generation runs from a GitHub Actions
-- lane, which is neither the UI nor the API. Widened in place (drop + re-add of the CHECK)
-- because the two existing labels are unchanged.
--
-- Security posture at creation, per migrations 237/447: RLS enabled with zero policies,
-- anon/authenticated DML revoked. All three relations are registered in
-- tests/test_migration_rls_grants.py::_ADMIN_ONLY_RELATIONS and
-- tests/test_tenant_isolation_live.py::_ADMIN_ONLY_RELATIONS. No `_public` view and none
-- planned — the SPA reads through the admin-gated API (`/new-dedup/candidates/*`).

------------------------------------------------------------------
-- candidate_inputs — one parameter set. `fingerprint` is the hash of the
-- canonical inputs JSON plus the generator version (toolkit/dedup_candidates.py
-- computes it; the DB only stores it), unique per path so the same inputs
-- always resolve to the same row.
------------------------------------------------------------------

create table if not exists dedup_sim.candidate_inputs (
  id                 bigserial primary key,
  path               text not null check (path in ('A', 'B', 'C')),
  fingerprint        text not null,
  generator_version  text not null,
  inputs             jsonb not null,
  created_at         timestamptz not null default now(),
  unique (path, fingerprint)
);

------------------------------------------------------------------
-- candidate_generations — one run of one parameter set. Mirrors
-- simulation_runs' status vocabulary; `progress` is the lane's resume
-- cursor (which blocks — towns, for path C — are done), `stats` the
-- funnel + audit numbers the Candidate audit page reads (computed once,
-- at the end of the run, never live over the pair table).
------------------------------------------------------------------

create table if not exists dedup_sim.candidate_generations (
  id                 bigserial primary key,
  simulation_run_id  bigint not null references dedup_sim.simulation_runs (id),
  inputs_id          bigint not null references dedup_sim.candidate_inputs (id),
  created_at         timestamptz not null default now(),
  started_at         timestamptz,
  completed_at       timestamptz,
  status             text not null default 'pending'
                     check (status in ('pending', 'running', 'success', 'failed')),
  progress           jsonb,
  stats              jsonb,
  error_message      text
);

create index if not exists candidate_generations_inputs_idx
  on dedup_sim.candidate_generations (inputs_id, created_at desc);

------------------------------------------------------------------
-- candidate_pairs — the store. PK (inputs_id, lo, hi) is the "pair ×
-- path × inputs" identity; `rung` is an attribute, not a key, because
-- under the fallback rule a pair is evaluated on exactly one rung per
-- path. `block_key` is what put the two listings in the same bucket
-- (path C: the obec_kod). The typed evidence columns are nullable: C1
-- fills `disposition`, C3 fills the area triple, and the floor columns
-- are filled whenever both sides are byt with a floor (`floor_checked`
-- says whether the ±N rule was actually applied).
------------------------------------------------------------------

create table if not exists dedup_sim.candidate_pairs (
  inputs_id       bigint not null references dedup_sim.candidate_inputs (id),
  listing_id_lo   bigint not null,
  listing_id_hi   bigint not null,
  rung            text not null,
  generation_id   bigint not null,
  block_key       text not null,
  category_type   text,
  category_main_lo text,
  category_main_hi text,
  disposition     text,
  area_lo         numeric,
  area_hi         numeric,
  area_diff_pct   numeric,
  floor_lo        integer,
  floor_hi        integer,
  floor_checked   boolean not null default false,
  first_seen_at   timestamptz not null default now(),
  last_seen_at    timestamptz not null default now(),
  primary key (inputs_id, listing_id_lo, listing_id_hi),
  check (listing_id_lo < listing_id_hi)
);

-- The only secondary index: the stale sweep after a successful run
-- ("rows of this inputs_id that this generation did not re-produce") and
-- the per-generation counts. Everything the audit page shows comes from
-- `candidate_generations.stats`, so no per-column index is paid for here.
create index if not exists candidate_pairs_generation_idx
  on dedup_sim.candidate_pairs (inputs_id, generation_id);

------------------------------------------------------------------
-- simulation_runs.triggered_by: + 'lane'.
------------------------------------------------------------------

alter table dedup_sim.simulation_runs
  drop constraint if exists simulation_runs_triggered_by_check;
alter table dedup_sim.simulation_runs
  add constraint simulation_runs_triggered_by_check
  check (triggered_by in ('ui', 'api', 'lane'));

------------------------------------------------------------------
-- Security posture.
------------------------------------------------------------------

alter table dedup_sim.candidate_inputs enable row level security;
alter table dedup_sim.candidate_generations enable row level security;
alter table dedup_sim.candidate_pairs enable row level security;

revoke all on dedup_sim.candidate_inputs from anon, authenticated;
revoke all on dedup_sim.candidate_generations from anon, authenticated;
revoke all on dedup_sim.candidate_pairs from anon, authenticated;
