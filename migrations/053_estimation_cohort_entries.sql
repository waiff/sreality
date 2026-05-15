-- 053_estimation_cohort_entries.sql
--
-- Side table for the agent cohort, so the server (not the LLM) is the
-- authoritative source of truth for "which sreality_ids ended up in
-- this estimation's cohort, and which the agent set aside".
--
-- Before this migration the agent had to retype every cohort
-- sreality_id back into `record_estimate.comparables_used`. LLMs
-- hallucinate IDs across long contexts (estimation 20 invented
-- 4253614156, 3826803788, 1453563980 and forgot the three round-3
-- additions). The current `_finalise` intersects declared IDs against
-- the in-memory cohort and silently drops the rest with a warning —
-- which is easy to miss, and the recorded `comparables_used` can be
-- a different cohort than the one the point estimate was computed on.
--
-- New model:
--
--   1. Every `find_comparables_relaxed` round upserts one row per
--      cohort listing into this table (denormalised attributes copied
--      so the row stands alone for analytical queries).
--   2. `_finalise` flips `present_at_finalisation` on rows whose
--      sreality_id is still in `state.last_cohort` at terminator time,
--      then applies `comparable_decisions` from the agent: any
--      decision='excluded' sets `excluded_by_agent=true` plus
--      `exclusion_reason`. Decisions referencing sreality_ids NOT in
--      the cohort (hallucinations) skip the table write and surface
--      as a warning on the run.
--   3. `comparables_used` (on `estimation_runs` and the API response)
--      becomes a server-derived projection: rows with
--      `present_at_finalisation=true AND excluded_by_agent=false`.
--   4. The trace JSONB's `selection_rounds` still carries the
--      per-round added/removed diffs for the frontend Strategy panel.
--      The side table is for the analytical / queryable view.
--
-- Why one row per (run, listing) rather than per (run, round, listing):
-- the analytical question is always "which listings ended up shaping
-- this estimate", not "which round first surfaced listing X". The
-- per-round trail is already in the trace JSONB. Single-row-per-listing
-- keeps the table small, the queries simple, and the UNIQUE constraint
-- enforced.

begin;

create table estimation_cohort_entries (
    estimation_run_id        bigint  not null
        references estimation_runs(id) on delete cascade,
    sreality_id              bigint  not null,
    first_seen_round_n       int     not null,
    last_seen_round_n        int     not null,
    snapshot_id              bigint,
    distance_m               double precision,
    price_czk                integer,
    area_m2                  double precision,
    price_per_m2             double precision,
    disposition              text,
    present_at_finalisation  boolean not null default false,
    excluded_by_agent        boolean not null default false,
    exclusion_reason         text,
    inclusion_reason         text,
    created_at               timestamptz not null default now(),
    updated_at               timestamptz not null default now(),
    primary key (estimation_run_id, sreality_id)
);

create index estimation_cohort_entries_run_present_idx
    on estimation_cohort_entries (estimation_run_id, present_at_finalisation);

create index estimation_cohort_entries_sreality_idx
    on estimation_cohort_entries (sreality_id);

alter table estimation_cohort_entries enable row level security;

create or replace function estimation_cohort_entries_touch_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

create trigger estimation_cohort_entries_touch_updated_at_trg
    before update on estimation_cohort_entries
    for each row execute function estimation_cohort_entries_touch_updated_at();

comment on table estimation_cohort_entries is
    'Server-authoritative log of which sreality listings the agent ''s '
    'cohort included for one estimation_run. Eliminates LLM ID '
    'transcription: the agent never types sreality_ids in '
    'record_estimate; the harness derives the included set from rows '
    'with present_at_finalisation=true AND excluded_by_agent=false.';

comment on column estimation_cohort_entries.present_at_finalisation is
    'True if this listing was still in state.last_cohort when '
    'record_estimate fired. False means the listing was added by an '
    'earlier round but a later round''s filter shift dropped it.';

comment on column estimation_cohort_entries.excluded_by_agent is
    'True when comparable_decisions for this sreality_id was '
    'decision=''excluded''. Together with present_at_finalisation this '
    'yields the final cohort used by the point estimate: '
    'present_at_finalisation=true AND excluded_by_agent=false.';

commit;
