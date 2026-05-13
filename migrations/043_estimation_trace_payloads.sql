-- 043_estimation_trace_payloads.sql
--
-- Phase AI slice A: trace inspection enrichment.
--
-- Architectural rule #9 keeps the trace JSONB on estimation_runs bounded:
-- each step stores only `output_summary`, never the full tool result.
-- That cap is what keeps rows in single-digit kilobytes regardless of
-- cohort size. Slice A adds a separate, lazily-loaded side-table so the
-- operator can drill from a tool_call step in the timeline into the
-- full payload that step returned (e.g. "find_comparables_relaxed
-- returned 42 listings; here are the 34 that didn't end up in
-- comparables_used and why").
--
-- One row per (estimation_run_id, step_n) pair. Written by the agent
-- loop and the deterministic estimation path as part of trace
-- finalisation; never updated. Old rows are safe to delete after 30
-- days (mirrors listing_freshness_checks discipline per architectural
-- rule #9 prose); no automated pruning is built, manual SQL when the
-- table grows. Removing an old row just removes the drill-down
-- ability for that step — the trace summary stays intact.
--
-- The estimation_runs FK uses ON DELETE CASCADE: estimation_runs are
-- themselves an audit trail and don't get deleted, but if one ever is
-- (operator cleanup), the orphaned payload rows should go with it.
--
-- RLS enabled with no policies — only the FastAPI service (service-
-- role connection) writes and reads here. The frontend reaches the
-- side-table exclusively via the bearer-gated
-- GET /estimations/{id}/trace/{n}/payload endpoint, same pattern as
-- all other private estimation surfaces.

create table estimation_trace_payloads (
  estimation_run_id bigint not null
    references estimation_runs (id) on delete cascade,
  step_n            int not null check (step_n >= 1),
  full_output       jsonb not null,
  captured_at       timestamptz not null default now(),
  primary key (estimation_run_id, step_n)
);

create index on estimation_trace_payloads (captured_at desc);

alter table estimation_trace_payloads enable row level security;
