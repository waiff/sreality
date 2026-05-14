-- 052_skill_versioning.sql
--
-- Operator follow-up to PR #90: surface which skill (and which
-- version of it) drove each estimation on the /estimations list.
-- Also exposes a "has feedback?" indicator on each row so the list
-- can render a clickable / disabled "feedback" affordance per run.
--
-- Skill versioning is the trickier piece. The existing
-- skills_history trigger from migration 029 already captures every
-- prior prompt; this migration adds an explicit integer `version`
-- that auto-increments on each UPDATE. Past runs that referenced a
-- skill carry their version forward by snapshotting onto
-- estimation_runs at finalisation time — even if the live skill
-- gets edited later, the run's `skill_version` keeps pointing at
-- the version that actually produced the estimate.
--
-- Changes:
--
--   1. `skills.version int not null default 1` and matching
--      `skills_history.version int`.
--   2. Replace `skills_record_history()` so it (a) carries the old
--      version into history, (b) carries the old `archived_at`
--      forward (added by migration 051; the prior trigger predates
--      it), and (c) bumps `new.version := old.version + 1` on
--      every meaningful update.
--   3. `estimation_runs.skill_name text` and
--      `estimation_runs.skill_version int` snapshot the choice at
--      run time. Both nullable: deterministic runs and pre-AI
--      agent runs have no skill to snapshot.
--   4. Backfill `skill_name` for agent runs that recorded a
--      skill_choice trace step (slice A.1 onwards). `skill_version`
--      stays null on backfill — the trace step doesn't carry a
--      version number and faking one would corrupt the audit.

begin;

------------------------------------------------------------------
-- 1. Add version columns
------------------------------------------------------------------

alter table skills
  add column if not exists version int not null default 1;

alter table skills_history
  add column if not exists version int;

comment on column skills.version is
  'Auto-incremented integer bumped on every UPDATE by the '
  'skills_history trigger. Used as a stable per-skill version '
  'identifier for the audit surface (no semver attempted).';

------------------------------------------------------------------
-- 2. Replace the history trigger function
------------------------------------------------------------------

create or replace function skills_record_history()
returns trigger
language plpgsql
as $$
begin
  insert into skills_history (
    name, description, system_prompt, allowed_tools,
    preferred_model, limits, replaced_at, replaced_by,
    archived_at, version
  )
  values (
    old.name, old.description, old.system_prompt, old.allowed_tools,
    old.preferred_model, old.limits, now(), old.updated_by,
    old.archived_at, old.version
  );
  new.updated_at := now();
  new.version    := old.version + 1;
  return new;
end;
$$;

-- Operator chose: every UPDATE bumps version, including
-- archive/unarchive flips. Drop the migration-029 WHEN clause so
-- the trigger fires on any row update. App code only writes when
-- fields actually change, so pure no-op updates aren't a
-- practical concern.

drop trigger if exists skills_history_trigger on skills;
create trigger skills_history_trigger
  before update on skills
  for each row
  execute function skills_record_history();

------------------------------------------------------------------
-- 3. estimation_runs snapshot columns
------------------------------------------------------------------

alter table estimation_runs
  add column if not exists skill_name    text default null,
  add column if not exists skill_version int  default null;

comment on column estimation_runs.skill_name is
  'Snapshot of the skill that produced this run, captured at '
  'finalisation. Null on deterministic runs. Survives later edits '
  'to the live skill (skills.name does not appear in '
  'skills_history, so this is the authoritative source for past-run '
  'attribution).';

comment on column estimation_runs.skill_version is
  'Snapshot of skills.version at finalisation time. Lets the '
  '/estimations list distinguish "ran under v1" from "ran under v2 '
  'after the operator applied a refinement". Null on deterministic '
  'runs and on agent runs predating migration 052 (the trace step '
  'did not carry a version number).';

------------------------------------------------------------------
-- 4. Backfill skill_name for agent runs with a skill_choice step
------------------------------------------------------------------

update estimation_runs
   set skill_name = (
     select s->'output_summary'->>'skill_name'
       from jsonb_array_elements(trace->'steps') s
      where s->>'kind'  = 'computation'
        and s->>'label' = 'skill_choice'
      limit 1
   )
 where mode = 'agent'
   and skill_name is null
   and trace is not null;

commit;
