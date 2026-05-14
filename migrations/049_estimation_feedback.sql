-- 049_estimation_feedback.sql
--
-- (Originally drafted as 047; bumped to 049 alongside the
-- 046 → 048_comparable_decisions and 048 → 050_skill_refinements
-- renames to clear slot 047 claimed by main's
-- skill_manual_estimates_tool work. Filename-only rename.)
--
-- Phase AI slice B: capture operator feedback on a specific
-- estimation run. The frontend collects a short free-text note;
-- slice C consumes these rows to drive the prompt refiner.
--
-- Schema:
--
--   id                 bigserial primary key
--   estimation_run_id  FK -> estimation_runs(id), ON DELETE CASCADE.
--                      Feedback is bound to its run; deleting the run
--                      (admin cleanup) drops its feedback too.
--   feedback_text      text NOT NULL. Operator's free-text. Length
--                      capped at 4000 chars by application code;
--                      no DB constraint because trimming a long
--                      note to fit is the API layer's job.
--   submitted_at       timestamptz NOT NULL default now().
--   status             text NOT NULL CHECK in (
--                          'submitted', 'refining', 'proposed',
--                          'applied', 'dismissed', 'failed'
--                      ).
--                      Lifecycle:
--                      submitted (default; row inserted by POST)
--                        → refining (kick_off_refinement=true)
--                          → proposed (refiner produced a draft)
--                            → applied | dismissed (operator decision)
--                          → failed (refiner errored)
--                        → dismissed (operator nuked it without ever
--                                     running the refiner)
--   refinement_id      int. FK to skill_refinements(id) added in
--                      migration 050 once that table exists. Null
--                      until the refiner produces a proposal.
--
-- Append-only in spirit (architectural rule #1) — rows mutate only
-- through controlled UPDATE statements as the feedback walks its
-- status lifecycle, never DELETE. The cascade is on the parent
-- estimation_run only.

begin;

create table if not exists estimation_feedback (
  id                bigserial primary key,
  estimation_run_id bigint not null references estimation_runs(id) on delete cascade,
  feedback_text     text not null,
  submitted_at      timestamptz not null default now(),
  status            text not null default 'submitted'
                    check (status in (
                      'submitted', 'refining', 'proposed',
                      'applied', 'dismissed', 'failed'
                    )),
  refinement_id     integer default null
);

comment on table estimation_feedback is
  'Operator feedback on a specific estimation_runs row. Slice B '
  'persists the note; slice C consumes it via the skill refiner.';

comment on column estimation_feedback.refinement_id is
  'FK to skill_refinements(id) added in migration 050. Null until '
  'the refinement pipeline produces a proposal.';

create index if not exists estimation_feedback_run_idx
  on estimation_feedback (estimation_run_id, submitted_at desc);

create index if not exists estimation_feedback_status_idx
  on estimation_feedback (status, submitted_at desc)
  where status in ('submitted', 'refining', 'proposed');

alter table estimation_feedback enable row level security;

-- Same posture as estimation_trace_payloads (migration 043) and
-- estimation_runs itself: service-role only; the frontend goes
-- through bearer-gated FastAPI routes that connect with the
-- service role. No policies are intentional — anon must not see
-- operator feedback.

commit;
