> Track file — part of [ROADMAP.md](../ROADMAP.md). After shipping, edit only this file + its index row.

## Skill refinement track (parallel)

Closing the loop on the Phase 7 agent: today the operator can edit a
skill's system prompt via `/settings`, but there is no structured way
to learn from a specific estimation that went well or badly. This
track adds (a) deeper trace inspection so the operator can actually
see *why* the agent picked the comparables it picked, and (b) a
feedback-driven prompt refinement loop where the operator's written
critique of a specific run gets fed back into the skill that produced
it.

### Phase AI: Feedback-driven skill refinement (active)

Sliced into three independent PRs along the data-flow boundary:
slice A captures full tool-call payloads alongside the existing
bounded trace; slice B adds operator feedback capture; slice C
drives the actual refiner skill. Each slice is independently
useful.

#### Slice A: Trace inspection enrichment (done)

Migration 043 lands the side-table foundation; PR1 of three.

- Migration 043: `estimation_trace_payloads(estimation_run_id,
  step_n, full_output jsonb, captured_at)`, PK on the pair.
  ON DELETE CASCADE so payloads track the parent run. RLS enabled,
  no policies — service-role only; the frontend reads via the
  bearer-gated endpoint below. 30-day retention documented in
  CLAUDE.md (architectural rule #9 prose); no automated pruner,
  manual SQL when the table grows.
- `TraceRecorder.set_full_output(...)` + `iter_payloads()` +
  top-level `flush_trace_payloads(conn, run_id, recorder)`. The
  recorder accumulates `(step_n, full_output)` pairs in memory;
  flush executes a single `executemany` INSERT after the parent
  `estimation_runs` row is persisted. `ON CONFLICT DO NOTHING`
  makes retry double-flush a no-op.
- Wired into:
  - `estimate_yield` (deterministic path): captures the full
    `find_comparables` cohort and `analyze_distribution` result.
  - `agent.run_agent_estimation`: captures every tool-call result
    in the loop, plus the terminator input and unknown-tool
    diagnostics. Exception paths leave the payload unset by
    design (failed tool calls have nothing to drill into).
  - All three persist sites: `create_estimation_run` success path,
    `_persist_failed_run`, and `_run_agent_path` (both finalise
    branches) call `flush_trace_payloads` after the row exists.
- `GET /estimations/{id}/trace/{n}/payload` (bearer-gated) returns
  `{step_n, full_output, captured_at}`, 404 when absent.
- Frontend `Timeline.tsx`: `tool_call` step bodies render a
  "Show full payload" expander that lazily calls the new
  `useTracePayload(runId, stepN, enabled)` hook (added to
  `frontend/src/lib/queries.ts`). `EstimationDetail` threads the
  run id into `<Timeline runId={run.id} />`; previews and other
  callers without a persisted run continue to render without the
  expander.
- Hermetic unit tests on `set_full_output` / `iter_payloads`:
  computation/reasoning steps never produce payload rows;
  numbering on payload rows lines up with the trace step `n`.

Past-run drill-down is one-directional in time: the writer only
captures payloads for runs executed *after* slice A shipped.
Pre-existing `estimation_runs` rows lose the drill-down ability;
the trace summary stays intact.

#### Slice A.1: Audit follow-ups (done)

Operator-driven adjustments to the trace surface uncovered the
moment slice A landed on /estimation/17:

- Migration 048 — `estimation_runs.comparables_excluded jsonb` and
  a string-replace UPDATE on both rental skill prompts inserting a
  required `comparable_decisions` bullet on the `record_estimate`
  arguments list. Applied via MCP; `skills_history` preserves the
  prior prompts.
- `record_estimate` schema (api/agent.py) accepts
  `comparable_decisions: [{sreality_id, decision, reason}]`. The
  agent's terminator step now records `n_comparables_included` /
  `n_comparables_excluded` in its bounded summary; the full
  decisions list lives in the slice A side-table.
- `_finalise` joins inclusion reasons onto each `comparables_used`
  entry (new optional `reason` field) and emits a parallel
  `comparables_excluded` list — both persisted on the run row.
- Agent loop emits a `skill_choice` computation step before the
  first LLM turn recording skill name, description, provider,
  model, limits, and tool whitelist — answers "why was this skill
  used" in the audit.
- Agent summary line wording: `after N iters` → `after N LLM
  turns` for clarity about what the counter represents.
- Frontend: `ComparableUsed.reason` + new `ComparableExcluded`
  type, "Why kept" column on the comparables table, "Considered
  and set aside" panel below it, and Mode / Skill / Model rows in
  the Inputs recap (pulled from the trace's `skill_choice` step).
- Hermetic tests on `_normalise_decisions` (malformed entries
  dropped, not raised) and on the agent trace shape (skill_choice
  always first).

These were follow-ups, not new slices — same PR.

#### Slice B: Feedback capture (done)

Migration 049 + the API surface land the operator's free-text
feedback as a first-class object linked back to the run:

- Migration 049 (applied via MCP): `estimation_feedback(id,
  estimation_run_id, feedback_text, submitted_at, status,
  refinement_id)` with a CHECK enum on `status` covering the full
  lifecycle (`submitted | refining | proposed | applied |
  dismissed | failed`). FK on the run cascades; RLS enabled, no
  policies — service-role only.
- `api/feedback.py` insert/get/list/update-status helpers (mirrors
  the small storage modules elsewhere in `api/`).
- `POST /estimations/{id}/feedback` accepts
  `{feedback_text, kick_off_refinement=true}` and either stashes
  the row (`status='submitted'`) or fires slice C inline
  (`status='refining'` → terminal status set by the refiner).
  `GET /estimations/{id}/feedback` returns the run's history,
  newest first.
- Frontend `FeedbackBlock` on `/estimation/:id`: composer with
  textarea + "Run the refiner now" checkbox, history list with a
  per-row status badge (FeedbackStatus), and per-row "View
  proposed change" expander that lazy-loads the slice C
  refinement.

#### Slice C: Refinement loop (done)

Same-skill, suggest-then-confirm. Prompt-only edits (operator's
choices in the slice-B/C kickoff).

- Migration 050 (applied via MCP):
  - `skill_refinements(id, skill_name FK skills, original_prompt,
    proposed_prompt, refiner_explanation, source_feedback_id FK
    estimation_feedback, status, created_at, applied_at)` —
    proposal lifecycle is `proposed → applied | dismissed`.
  - FK from `estimation_feedback.refinement_id` →
    `skill_refinements(id)`, `ON DELETE SET NULL`.
  - `llm_calls.called_for` CHECK extended with `refine_skill`.
  - `app_settings.llm_skill_refiner_system_prompt` +
    `llm_skill_refiner_model` seeds (operator can edit live).
  - `skills.skill_refiner_v1` seed row with prompt-only tool
    whitelist (`["record_skill_refinement"]`) and tight limits
    (`max_iterations=2`, `max_cost_usd=0.40`,
    `wall_clock_timeout_s=60`).
- `skills/skill_refiner_v1/SKILL.md` canonical docs.
- `api/refiner.py` — single-pass LLM call, parses the run's
  `skill_choice` trace step to discover which skill produced it
  (deterministic / pre-slice-A.1 runs report a soft 'failed'
  status), assembles the refiner user message from the original
  prompt + feedback + compacted trace, calls
  `LLMClient.call(called_for='refine_skill')`, and persists the
  proposal. Helpers `apply_refinement` / `dismiss_refinement` flip
  both the refinement and its parent feedback row; applying goes
  through `skills.update_skill` so the existing `skills_history`
  trigger from migration 029 preserves the prior prompt.
- `GET /skill-refinements/{id}` and
  `POST /skill-refinements/{id}/decision` (apply | dismiss),
  bearer-gated.
- Frontend: `RefinementProposal` renders the refiner's explanation,
  a line-based prompt diff (green = added, red = removed), and
  Apply / Dismiss buttons when the proposal is still in `proposed`
  state. Diff is computed client-side from `original_prompt` and
  `proposed_prompt`.
- Hermetic tests (`tests/api/test_refiner.py`) cover the pure
  helpers: `_pick_skill_name_from_run`,
  `_build_refiner_user_message`, `_compact_steps`.

**Caveats:**

- Refiner can only act on agent-mode runs (deterministic runs
  have no skill to refine). The lifecycle handles this by setting
  the feedback's status to `failed` and never producing a
  refinement row.
- Past-run feedback works if the run has a `skill_choice` step in
  its trace (i.e. ran under slice A.1 or later). Older agent runs
  return `failed` with the same handling.
- `auto_apply_refinements` flag is intentionally NOT implemented —
  operator chose suggest-then-confirm; auto-apply can be a Phase
  AI follow-up if same-session iteration feels slow.

#### Slice C.1: Skill consolidation (done)

Operator follow-up: two active rental skills (`rental_estimator_v1`
and `rental_estimator_full_v1`) was confusing UX. Decision: keep
only the full skill active, treat the older one as history. The
refiner pipeline updates the full skill in place going forward —
`skills_history` is the per-skill audit trail, no new sibling
skill rows.

- Migration 051 (applied): `skills.archived_at timestamptz`. Same
  column on `skills_history` so snapshots preserve the archival
  state. `rental_estimator_v1.archived_at` set to now().
- Backend default: `CreateEstimationIn.skill` flipped from
  `"rental_estimator_v1"` to `"rental_estimator_full_v1"`. Existing
  estimations referencing v1 in their trace still load v1 (load_skill
  doesn't filter by archival); only new estimations and the default
  picker are gated.
- `list_skills(conn, include_archived=False)` is the new shape;
  `GET /admin/skills?include_archived=true` exposes archived rows.
  Frontend `Skill.archived_at` + a "Show archived skills" toggle on
  the Settings page. Archived cards render with a muted background
  and a small `archived` tag.

The "skill picker on the new-estimation modal" was scoped out —
the operator's mental model is now: one canonical rental skill,
refined in place via slice C, with history per-skill in
`skills_history`. Past runs that ran under v1 keep their trace's
`skill_choice` step pointing at v1; the row is still there for them
to load.

#### Slice D: Multi-pass estimation strategy + confidence revision (proposed)

Two related changes to the estimator skill's behaviour, separate
from the refiner loop but in the same family of "make the agent's
reasoning loop richer." Independent of slices A–C; can ship in
parallel.

**Multi-pass strategy**

Today's `rental_estimator_v1` runs a single pass: pick filters,
fetch comparables, compute the distribution, emit a point estimate.
The revised flow runs three explicit iterations:

1. **Reconnaissance.** Inspect the available sample for the
   candidate filter spec — how many comparables exist, how
   dispersed they are, whether obvious gaps (only top-floor flats,
   only renovated units, etc.) constrain inference. Output: one or
   more declared benchmarking strategies and the reasoning behind
   each. A "strategy" here is a concrete plan ("widen radius to
   1.5 km and trim 10/90", "find the two best-matched units and
   anchor on those", "split the cohort by floor band and average").
2. **Execution.** Run each declared strategy end to end (gather
   the cohort it implies, compute its estimate, capture its
   confidence inputs). Strategies that fall through — e.g.
   "find two near-identical units" returns zero — fail open and
   don't gate the run.
3. **Adjudication.** Compare the strategies' results and pick the
   one the agent judges most reliable. The chosen strategy's
   estimate is the run's primary result; the others are recorded
   as alternates in the trace so the operator can inspect what
   was considered and why it was rejected.

Iterate from step 1 until the chosen strategy reaches at least
medium confidence per the revised score below, bounded by the
skill's `max_iterations` and `max_cost_usd` so a stubborn run
can't spend unbounded LLM credit. Hitting the bound returns the
best-so-far estimate with the confidence label it actually
earned (no rounding up).

Trace shape: each iteration emits a `reasoning` step explaining
the chosen strategy followed by the `tool_call` steps it needs.
`TRACE_SCHEMA_VERSION` in `api/estimation_runs.py` bumps when
this lands. Alternate-strategy summaries (estimate, confidence,
reason for not picking) live in a new `alternate_strategies`
array on the trace summary, kept small per architectural rule #9
— full cohorts go to `estimation_trace_payloads` (Slice A) so
each step's `output_summary` stays a summary.

**Confidence revision**

Today's confidence label is dominated by sample size. The revised
calculation factors in:

- **Quality of fit.** Two near-identical comparables (same
  disposition, same micro-location, same floor band, same
  condition, similar age) can warrant higher confidence than
  fifty loose matches. Today this signal is implicit in the IQR;
  it becomes an explicit input — a per-comparable "match score"
  derived from facet overlap with the subject listing, with the
  cohort's mean/min match score feeding the confidence calc.
- **Sample size.** Still relevant — a single comparable is fragile
  no matter how well-matched. The new formula doesn't drop sample
  size, it stops letting sample size alone determine the label.
- **Cross-strategy agreement.** When two independent strategies
  (e.g. "narrow filter" vs. "broad filter trimmed to outliers")
  produce estimates within a configurable epsilon (default ~5 %),
  confidence rises; wide disagreement lowers it. Only emitted when
  at least two strategies survived adjudication.
- **Freshness.** Already captured in metadata, but factor it into
  the label so a cohort full of stale comparables can't masquerade
  as high-confidence.

Labels stay `low | medium | high`. The label-shape change is
coordinated across `api/schemas.py`, `toolkit/comparables.py`,
the agent skill's prompt, and the `/estimation/:id` UI — capture
as one migration when the score components persisted on
`estimation_runs` change shape. `estimation_runs.confidence_score`
gets a sibling `confidence_breakdown jsonb` so the operator can
see which signal drove the label.

**Skill / prompt impact**

- `rental_estimator_v1` system prompt is rewritten to teach the
  three-iteration flow and the new strategy vocabulary.
  `app_settings_history` (migration 020) preserves the prior
  prompt automatically when the operator writes through
  `PUT /admin/skills/{name}`. Bump the seed `INSERT` migration's
  comment to flag the format change; do **not** edit the original
  migration (architectural rule #1).
- Building decomposition (Phase B2) fans out per-unit through the
  same apartment estimator skill, so Slice D's iteration changes
  propagate automatically — no separate building-level rework. The
  building rollup view continues to aggregate the per-unit estimates
  as they are produced; no new logic at the building tier.

**Out of scope for this slice**

- Auto-tuning the strategy mix from past runs — that's Slice C's
  refiner once it has data to learn from.
- Operator-visible per-strategy A/B at the skill level — Phase 7
  slice 2 covers that for skills as a whole.
- New strategies beyond what the prompt enumerates. The skill
  picks from a written list; expanding the list is a prompt edit,
  not a code change.

---

#### Original phase brief (pre-slicing)

**Trace inspection enrichment**

The existing trace already records every tool call's parameters and
an `output_summary` per architectural rule #9 (capping row size at
single-digit kilobytes regardless of cohort size). What's missing is
the ability to drill from a tool call's row in the timeline into the
full payload it returned — concretely, the operator wants to see
"this `find_comparables_relaxed` call with these filters returned 42
listings; the 8 that ended up in `comparables_used` were picked
because of this reasoning step; here are the 34 that didn't make
the cut and why."

- Trace step rows in the UI already render `filters_used` from the
  tool's metadata envelope (per toolkit rule #2). What they don't
  render: the listings that came back from the call but weren't
  selected. Two viable shapes:
  - **Shape A — payload side-table.** New table
    `estimation_trace_payloads(estimation_run_id, step_n,
    full_output jsonb, captured_at)` written at trace-finalisation
    time. Architectural rule #9 stays intact (the trace JSONB on
    `estimation_runs` keeps only the summary); this is a separate,
    lazily-loaded record. UI fetches `/estimations/{id}/trace/{n}/payload`
    on click-to-expand. Recommended default.
  - **Shape B — on-demand re-execution.** No new storage; the UI
    re-runs the tool with the recorded params. Cheaper at write
    time but breaks the freshness contract — the listings table
    moves under the agent's feet, so the operator sees a different
    cohort than the run actually used. Bad for the audit story.
- The Timeline component (`frontend/src/components/Timeline.tsx`)
  already dispatches on `step.kind`; this is a new render mode on
  `tool_call` steps that exposes the expandable payload view +
  per-listing "did this make `comparables_used`? if not, why?"
  annotation.
- The "why" annotation per non-selected listing comes from the
  reasoning step that immediately follows the tool call (per the
  Phase 7 slice 1 trace shape — reasoning kind is emitted per LLM
  turn). The UI surfaces the relevant slice of that reasoning
  alongside the listings table.

**Feedback capture**

- Migration: `estimation_feedback(id, estimation_run_id, feedback_text,
  submitted_at, status, refinement_id)` — one row per operator
  feedback submission, linked back to the run. `status` lifecycle:
  `submitted` → `refining` → `proposed` | `applied` | `dismissed` |
  `failed`. Append-only (architectural rule #1 spirit even though
  this is operational data, not history).
- API: `POST /estimations/{id}/feedback` accepts
  `{feedback_text: str, kick_off_refinement: bool = true}` and
  inserts a row. Bearer-gated. Defaults to immediately kicking off
  the refinement loop so the operator gets a same-session proposal;
  setting the flag false stores the feedback without spending LLM
  credit.
- Frontend: a "Provide feedback" button sits alongside the existing
  "Re-run" button on `/estimation/:id`. Click opens a modal with a
  textarea + submit. Past feedback for a run renders inline (one
  block per submission, status badge, link to the proposed
  refinement when applicable).

**Refinement loop**

- New skill `skill_refiner_v1` (on-disk seed
  `skills/skill_refiner_v1/SKILL.md` + migration seed `INSERT`,
  same pattern as `rental_estimator_v1` per Phase 7 slice 1). Input
  context: the original skill (system prompt + allowed tools +
  preferred model + limits, sourced fresh from the `skills` row at
  refinement time), the full estimation trace including the new
  trace payloads, and the operator's feedback text. Output: a
  proposed updated `system_prompt` (and optionally an updated
  `allowed_tools` whitelist when the feedback says "stop using
  tool X" or "you should have used tool Y"), plus a one-paragraph
  explanation of what the refiner changed and why.
- Limits: `max_iterations: 2`, `max_cost_usd: 0.40`,
  `wall_clock_timeout_s: 60`. The refiner is a single reasoning
  pass over a fully-materialised context, not an iterative tool-
  use loop, so limits sit lower than the estimator's.
- Calls log to `llm_calls` with `called_for='refine_skill'` (new
  value on the CHECK constraint via the same migration).

**Apply vs. suggest — the safety-critical choice**

- **Default: suggest-then-confirm.** The refiner writes the proposed
  new prompt to a new staging table `skill_refinements(id, skill_id,
  original_prompt, proposed_prompt, proposed_allowed_tools,
  refiner_explanation, source_feedback_id, status, created_at,
  applied_at)`. Status: `proposed` → `applied` | `dismissed`. The
  operator reviews the diff on `/settings/skills/{name}/refinements`
  and clicks Apply (which writes through `PUT /admin/skills/{name}`,
  letting the existing `skills_history` trigger from migration 029
  preserve the prior value automatically) or Dismiss.
- **Optional: auto-apply.** Operator can flag a skill as
  `auto_apply_refinements: true` via `/settings`. Useful for early
  iteration when the operator wants tight loops; risky in the long
  run because LLM-written prompt edits will drift the skill's
  behaviour silently. Strongly recommend leaving this off in
  production.
- Either path goes through `skills_history` for full audit and
  rollback, same discipline as `app_settings_history` (migration
  020).

**Open questions (operator to decide before implementation starts)**

- **Payload retention.** How long do we keep `estimation_trace_payloads`
  rows? Forever bloats the table (a single run's payload can be
  hundreds of KB); 30 days mirrors `listing_freshness_checks` and
  is the recommended default. Old rows just remove the
  drill-down ability — the trace summary stays intact.
- **Refinement scope.** Does the refiner update the *same* skill the
  run used, or fork to a new `_v2`/`_vN` skill so the original stays
  pristine for A/B comparison? Forking is heavier but matches how
  the Phase 7 slice 2 A/B view assumes multiple skill variants.
- **Allowed-tools edits.** Should the refiner be allowed to change
  the tool whitelist, or only the system prompt? Prompt-only is
  simpler and harder to break things with; tool-whitelist edits
  unlock real behaviour change but need stricter validation
  (refusing to whitelist a tool that doesn't exist, etc.).
- **Feedback batching.** Apply each feedback submission individually
  (chatty, fast iteration, more LLM cost), or accumulate N
  submissions and refine once over the bundle (cheaper, slower
  iteration)? Default: per-submission, behind the
  `kick_off_refinement` flag so the operator can batch manually.
- **Default model for the refiner.** A capable model (Claude Opus,
  GPT-4o, Gemini Pro) is worth the cost here — it's writing prompts
  that drive every subsequent estimation. Lock to a specific model
  via `app_settings.llm_skill_refiner_model` so the operator can
  swap without redeploying.

**Out of scope for Phase AI**

- Automated regression testing of refined skills (re-running the
  refined skill against a fixture set of past estimations to check
  for behaviour drift) — that's a follow-up phase once the basic
  loop is in place.
- Multi-operator feedback aggregation — today's single-operator
  identity model applies (same as Phase U2.7).
- Cross-skill refinement ("learning from the rental skill should
  improve the sale skill") — out of scope; each skill is refined
  in isolation against its own runs.

