-- 034_estimation_runs_provider_skill.sql
--
-- Promotes the agent's provider + skill name from trace-JSON-only to
-- first-class columns on estimation_runs. This is the schema half of
-- Phase 7 slice 2: with these columns the list page can render chips
-- per run, filter by provider/skill, and the A/B compare view can
-- group sibling re-runs without parsing trace JSON.
--
-- Deterministic runs leave both NULL (correct — no provider/skill
-- was selected for those rows). Agent runs going forward write both
-- in api.estimation_runs._insert_run / _update_run_terminal.
--
-- Backfill: pre-slice-2 agent runs recorded provider + skill in the
-- trace.metadata block via the agent_summary_line helper in
-- api/estimation_runs.py:704-720. Where that JSON path is populated
-- we recover the values; rows where the metadata never made it to
-- the trace stay NULL and are correctly visible as such on the list.
--
-- No public view: the SPA reads estimation_runs through the FastAPI
-- service, not Supabase REST, so no anon grant is needed.

alter table estimation_runs
  add column provider   text,
  add column skill_name text;

create index estimation_runs_provider_idx
  on estimation_runs (provider)
  where provider is not null;

create index estimation_runs_skill_name_idx
  on estimation_runs (skill_name)
  where skill_name is not null;

update estimation_runs
   set provider   = trace->'metadata'->>'provider',
       skill_name = trace->'metadata'->>'skill'
 where mode = 'agent'
   and provider is null;

-- Slice-1 agent runs predate the structured trace.metadata block,
-- so trace.metadata is NULL on those rows. The provider + skill are
-- still recoverable from trace.summary, which is built by
-- api.estimation_runs._agent_summary_line as
-- "agent <provider>/<skill> after ..." or "agent failed: ...". A
-- failed run before it logged the summary line leaves both NULL,
-- which is correct (no provider/skill ever selected for that row).
update estimation_runs
   set provider   = m[1],
       skill_name = m[2]
  from (
    select id,
           regexp_match(trace ->> 'summary',
                        '^agent ([a-z_]+)/([a-z0-9_]+) ') as m
      from estimation_runs
     where mode = 'agent' and provider is null
  ) sub
 where estimation_runs.id = sub.id
   and sub.m is not null;
