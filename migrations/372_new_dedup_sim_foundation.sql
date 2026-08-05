-- 372_new_dedup_sim_foundation.sql
--
-- NEW DEDUP Wave 1 — schema foundation for the simulation engine. Design:
-- docs/design/new-dedup/PROGRAM.md ("Simulation architecture", Q15).
--
-- Schema `dedup_sim` is droppable wholesale (Wave 8, once the real engine
-- is productionized and writes through `merge_properties` directly) —
-- every sim table lives here, never in the public schema, so teardown is
-- a single `drop schema dedup_sim cascade`. Nothing in this schema is
-- ever read by production paths (Browse, watchdogs, estimation).
--
-- Evidence-tier tables (candidate pairs, per-tag pHash/embedding
-- comparisons) and the decision/group tables a run actually produces are
-- NOT part of this migration — each lands with the wave that needs it
-- (candidates: Wave 2; pHash evidence: Wave 4; embedding evidence: Wave
-- 5), so this migration stays the minimal Wave-1 foundation: the
-- settings registry's DB half, and the run-tracking table every later
-- wave's decision tier will insert into.

create schema dedup_sim;

------------------------------------------------------------------
-- settings — operator overrides only. Defaults, categories, value
-- types, constraints, and the mission-mandated plain-language blurb
-- for each knob live in code (toolkit/dedup_sim_settings.py), the same
-- split `toolkit/filter_registry.py` + `filter_visibility` (migration
-- 059) already uses in this codebase: a missing row here means "use
-- the registry default," so shipping a new setting in a later wave
-- never needs its own migration. Shape otherwise mirrors app_settings
-- (migration 020) — value + a trigger-recorded history for rollback.
------------------------------------------------------------------

create table dedup_sim.settings (
  key         text primary key,
  value       jsonb not null,
  updated_at  timestamptz not null default now(),
  updated_by  text
);

alter table dedup_sim.settings enable row level security;

create table dedup_sim.settings_history (
  id           bigserial primary key,
  key          text not null,
  value        jsonb not null,
  replaced_at  timestamptz not null default now(),
  replaced_by  text
);

create index on dedup_sim.settings_history (key, replaced_at desc);

alter table dedup_sim.settings_history enable row level security;

create function dedup_sim.settings_record_history()
returns trigger
language plpgsql
as $$
begin
  insert into dedup_sim.settings_history (key, value, replaced_at, replaced_by)
  values (old.key, old.value, now(), old.updated_by);
  return new;
end;
$$;

create trigger settings_record_history
  before update on dedup_sim.settings
  for each row
  when (old.value is distinct from new.value)
  execute function dedup_sim.settings_record_history();

------------------------------------------------------------------
-- simulation_runs — one row per decision-tier recompute ("Run dedup
-- simulation"). Snapshots the full settings JSON that produced it (a
-- run stays reproducible even after settings change later), so this
-- is the single source of truth for what any given run's numbers mean
-- — mirrors estimation_runs' shape (migration 010), this codebase's
-- established run-tracking convention. `stats` is deliberately
-- permissive jsonb (funnel counts, cost estimates, per-level tallies)
-- since its shape grows with each wave; convention enforced in code,
-- same as estimation_runs.trace.
------------------------------------------------------------------

create table dedup_sim.simulation_runs (
  id            bigserial primary key,
  created_at    timestamptz not null default now(),
  started_at    timestamptz,
  completed_at  timestamptz,

  status        text not null default 'pending'
    check (status in ('pending', 'running', 'success', 'failed')),
  triggered_by  text not null default 'api'
    check (triggered_by in ('ui', 'api')),

  settings_snapshot  jsonb not null,
  stats              jsonb,
  error_message      text
);

create index on dedup_sim.simulation_runs (created_at desc);
create index on dedup_sim.simulation_runs (status);

alter table dedup_sim.simulation_runs enable row level security;
