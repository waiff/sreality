-- 010_estimation_runs.sql
-- Persistent record of every estimation run. Purposes:
--   1. UI can list and replay past runs.
--   2. ClickUp / external triggers create rows the UI can browse.
--   3. Agent runs (Phase U4) write their reasoning trace into the same
--      table; deterministic runs just write a simpler trace.
--   4. Audit trail: every estimate is reconstructable from its
--      comparables_used (snapshot IDs) and trace.
--
-- Status lifecycle:
--   Synchronous deterministic mode goes straight to a terminal status
--   ('success' or 'failed'). The row is INSERTed once, after the
--   estimate has been computed, so 'pending'/'running' are unused for
--   today's path. The column allows them so Phase U4's async agent
--   runs can INSERT 'pending', UPDATE to 'running', then UPDATE to
--   terminal — with no schema change required.
--
-- Source / mode as TEXT with CHECK constraints:
--   We expect to add new sources ('mcp', 'slack', etc.) without a
--   dedicated migration each time. CHECK is cheap to relax with a
--   single ALTER. A native enum would force a CREATE TYPE migration
--   per addition.
--
-- comparables_used shape (jsonb):
--   list of {sreality_id, snapshot_id, snapshot_date, data_age_days,
--   verified_during_estimate}. Mirrors what /estimate_yield already
--   returns; we just persist verbatim.
--
-- trace shape (jsonb): see api/estimation_runs.py docstring constant
--   TRACE_SCHEMA_VERSION. Permissive on purpose; convention is enforced
--   in code.
--
-- RLS enabled; NO policies. The frontend reads runs through the API,
-- not directly via Supabase anon (deliberate departure from the U1a
-- "anon reads listings" pattern, because the API enforces auth and
-- shapes the response). When/if we add anon-read it goes in a later
-- numbered migration.

create table estimation_runs (
  id                          bigserial primary key,
  created_at                  timestamptz not null default now(),

  source                      text not null
    check (source in ('ui', 'api', 'clickup')),
  mode                        text not null
    check (mode in ('deterministic', 'agent')),
  status                      text not null
    check (status in ('pending', 'running', 'success', 'failed')),

  input_url                   text,
  input_sreality_id           bigint,
  input_spec                  jsonb not null,
  input_purchase_price_czk    integer,

  estimated_monthly_rent_czk  integer,
  rent_p25_czk                integer,
  rent_p75_czk                integer,
  gross_yield_pct             numeric(5,2),
  confidence                  text
    check (confidence is null or confidence in ('high', 'medium', 'low')),

  comparables_used            jsonb,
  trace                       jsonb,
  warnings                    jsonb,
  error_message               text,

  parent_run_id               bigint
    references estimation_runs(id) on delete set null,
  rerun_reason                text
);

create index on estimation_runs (created_at desc);
create index on estimation_runs (source, created_at desc);
create index on estimation_runs (input_sreality_id);
create index on estimation_runs (status);
create index on estimation_runs (parent_run_id);

alter table estimation_runs enable row level security;
