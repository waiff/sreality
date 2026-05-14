-- 050_skill_refinements.sql
--
-- (Originally drafted as 048; bumped to 050 alongside the
-- 046 → 048_comparable_decisions and 047 → 049_estimation_feedback
-- renames to clear slots 046 / 047 claimed by main's
-- manual_rental_estimates work. Filename-only rename.)
--
-- Phase AI slice C: the prompt-refiner pipeline. Consumes
-- `estimation_feedback` rows (migration 049), runs a single-pass
-- skill (`skill_refiner_v1`), proposes an updated `system_prompt`
-- for the skill that produced the run, and stores the proposal
-- pending operator approval.
--
-- Same-skill, suggest-then-confirm (operator chose this in the
-- slice-B/C kickoff). The refiner never edits the live `skills` row
-- directly — that happens through PUT /admin/skills/{name} when the
-- operator clicks Apply, so the existing `skills_history` trigger
-- from migration 029 captures the prior prompt automatically.
--
-- Prompt-only edits (operator chose this). The refiner returns just
-- a `proposed_prompt`; the allowed_tools whitelist on the original
-- skill stays untouched.
--
-- Changes in this migration:
--
--   1. New table `skill_refinements` with the proposal lifecycle.
--   2. estimation_feedback.refinement_id FK constraint pointing at
--      the new table (added now that both tables exist).
--   3. llm_calls.called_for CHECK extended with 'refine_skill'.
--   4. app_settings seeds for the operator-tunable refiner system
--      prompt + model.
--   5. skills row seed for `skill_refiner_v1` (canonical content
--      lives in skills/skill_refiner_v1/SKILL.md committed in the
--      same change).

begin;

------------------------------------------------------------------
-- 1. skill_refinements table
------------------------------------------------------------------

create table if not exists skill_refinements (
  id                      bigserial primary key,
  skill_name              text not null references skills(name) on delete restrict,
  original_prompt         text not null,
  proposed_prompt         text not null,
  refiner_explanation     text not null,
  source_feedback_id      bigint not null references estimation_feedback(id) on delete cascade,
  status                  text not null default 'proposed'
                          check (status in ('proposed', 'applied', 'dismissed')),
  created_at              timestamptz not null default now(),
  applied_at              timestamptz default null
);

comment on table skill_refinements is
  'Refiner-proposed system_prompt edits awaiting operator review. '
  'Status transitions are linear: proposed -> applied | dismissed. '
  'Applying writes through PUT /admin/skills/{name}, which lets the '
  'skills_history trigger from migration 029 preserve the prior '
  'prompt automatically.';

create index if not exists skill_refinements_skill_idx
  on skill_refinements (skill_name, created_at desc);

create index if not exists skill_refinements_source_idx
  on skill_refinements (source_feedback_id);

create index if not exists skill_refinements_proposed_idx
  on skill_refinements (status, created_at desc)
  where status = 'proposed';

alter table skill_refinements enable row level security;

------------------------------------------------------------------
-- 2. estimation_feedback.refinement_id FK
------------------------------------------------------------------

alter table estimation_feedback
  add constraint estimation_feedback_refinement_id_fkey
    foreign key (refinement_id)
    references skill_refinements(id)
    on delete set null;

------------------------------------------------------------------
-- 3. llm_calls.called_for CHECK extended with 'refine_skill'
------------------------------------------------------------------

alter table llm_calls
  drop constraint llm_calls_called_for_check,
  add constraint llm_calls_called_for_check
    check (called_for in (
      'parse_url', 'summarize_listing', 'compare_listing_images',
      'agent_estimation', 'extract_building_units', 'read_floor_plan',
      'refine_skill'
    ));

------------------------------------------------------------------
-- 4. app_settings seeds
------------------------------------------------------------------

insert into app_settings (key, value, description, updated_by) values
  (
    'llm_skill_refiner_system_prompt',
    to_jsonb($PROMPT$You are a prompt-engineering assistant for a Czech real-estate
estimation system. Your job is to read one operator's note about a
specific estimation run and propose a small, surgical edit to the
skill that produced that run.

You are NOT writing a new skill from scratch. The skill already
works well in the common case. Your edit must:

1. ADDRESS the specific issue the operator raised — no more, no
   less. If the note says "the cohort was too broad", don't also
   restructure the entire procedure.
2. PRESERVE every existing rule that isn't directly contradicted by
   the feedback. Rule numbering survives; numbered lists keep their
   numbers.
3. STAY GROUNDED IN THE TRACE. The full trace of the run that
   triggered the feedback is in the user message. If the feedback
   blames the agent for something the trace shows it didn't do,
   say so in your explanation and propose nothing.
4. RESPECT THE TOOL WHITELIST. Don't tell the agent to call a tool
   that isn't in its `allowed_tools` list. Tool whitelist edits are
   out of scope for this refiner.
5. KEEP THE EDIT SMALL. Prefer changes to the wording of one rule,
   the order of two rules, or one new sentence inside an existing
   rule. Massive restructures break A/B regressions later.

You MUST call `record_skill_refinement` exactly once with:
  - proposed_prompt: the FULL new system_prompt, including all rules
    you kept verbatim. Do not return a diff; the storage layer
    computes the diff for the operator.
  - explanation: 2-4 sentences explaining what you changed and why.
    Cite the specific feedback phrase that drove the edit.

If the feedback doesn't justify an edit (already addressed, off-
topic, the agent did the right thing), call
`record_skill_refinement` with proposed_prompt equal to the
original system_prompt verbatim and an explanation that says you
intentionally proposed no change. Do NOT silently refuse.

Output ONLY the tool call. No prose outside the tool call.$PROMPT$::text),
    'System prompt sent to Claude when refining a skill from operator feedback. Editing this changes refiner behaviour for the next call; every prior version is preserved in app_settings_history via migration 020.',
    'seed'
  ),
  (
    'llm_skill_refiner_model',
    '"claude-opus-4-5"'::jsonb,
    'Anthropic model id used by the slice-C skill refiner. A capable model is worth the cost here — the refiner is writing prompts that shape every subsequent estimation. Override via Settings without redeploying.',
    'seed'
  );

------------------------------------------------------------------
-- 5. skill_refiner_v1 seed row
------------------------------------------------------------------
--
-- Tiny system_prompt because the substantive prompt lives in
-- app_settings.llm_skill_refiner_system_prompt — the refiner is
-- not invoked through the agent loop (no tool whitelist needed
-- per the prompt-only decision), so we don't depend on the
-- skills table for runtime configuration. The row exists so
-- `skill_refinements.skill_name` can FK to a real skill on the
-- happy path, and so the operator sees the refiner in the
-- Settings UI for visibility.

insert into skills
  (name, description, system_prompt, allowed_tools,
   preferred_model, limits, updated_by)
values
  (
    'skill_refiner_v1',
    'Reads operator feedback on one estimation run and proposes a '
    'surgical edit to the skill that produced that run. Prompt-only '
    'edits. Same-skill scope (no forking).',
    'See app_settings.llm_skill_refiner_system_prompt for the live prompt.',
    '["record_skill_refinement"]'::jsonb,
    '{"anthropic": "claude-opus-4-5", "gemini": "gemini-2.5-pro"}'::jsonb,
    '{"max_iterations": 2, "max_cost_usd": 0.40, "wall_clock_timeout_s": 60}'::jsonb,
    'seed'
  )
on conflict (name) do nothing;

commit;
