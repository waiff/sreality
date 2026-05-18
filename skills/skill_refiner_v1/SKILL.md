---
name: skill_refiner_v1
description: Reads operator feedback on one estimation run and proposes a surgical edit to the skill that produced that run. Prompt-only edits. Same-skill scope (no forking).
allowed_tools:
  - record_skill_refinement
preferred_model:
  anthropic: claude-opus-4-5
  gemini: gemini-2.5-pro
limits:
  max_iterations: 2
  max_cost_usd: 0.40
  wall_clock_timeout_s: 60
---

# skill_refiner_v1 — canonical content

This is the Phase AI slice C skill. Migration 050 seeded the row;
migration 071 updates the live refiner prompt to v2.

Unlike the rental estimator, this skill does **not** drive the
agent loop in `api/agent.py`. The actual system prompt for the
refinement call comes from
`app_settings.llm_skill_refiner_system_prompt` (so the operator
can iterate on the refiner's behaviour via the Settings page
without redeploying), and the model id from
`app_settings.llm_skill_refiner_model`. The `skills` row exists so
`skill_refinements.skill_name` has a valid FK target and so the
refiner appears in the Settings inventory.

## v2 behaviour summary

The v2 refiner prompt teaches two things the v1 prompt didn't:

1. **Structure preservation.** Modern skill prompts (e.g.
   `rental_estimator_full_v1`'s v2) use a clearly-labelled section
   tree — Mission, ideal-cohort north star, compromise rules,
   operating principles, suggested moves. The refiner now reads
   that structure, slots every edit into the section it belongs
   in, and never invents new top-level sections, collapses
   sections, or reorders them. Rule numbering survives; section
   order survives.

2. **Constructive translation.** Vague feedback ("this estimate
   is off") is no longer a free pass to refuse. The refiner reads
   the trace to identify the most plausible root cause and
   addresses *that* in the appropriate section. The no-change
   path is reserved for cases where an edit truly would NOT
   improve estimates (off-topic, agent did the right thing, root
   cause is data not prompt).

Operating principles (lifted from the live `app_settings` row;
keep in sync if you ever change either side):

1. Place edits in the right section. Ideal-cohort feedback →
   "Ideal cohort"; compromise feedback → "How to compromise";
   new hard rules → "Operating principles"; new tool plays →
   "Suggested moves"; threshold tweaks → wherever the threshold
   lives today.
2. Preserve everything not directly contradicted by the feedback.
3. Translate feedback constructively. Vague → diagnose from the
   trace and address the root cause. Outcome complaints → work
   backwards to a prompt-level fix.
4. Stay grounded in the trace. Disagree if the trace doesn't
   support the feedback.
5. One edit per feedback. Don't bundle improvements.
6. Respect the tool whitelist. Tool whitelist edits are out of
   scope.

The refiner returns the FULL new system_prompt (not a diff) plus
a 2-4 sentence explanation that names (a) which section was
edited, (b) what changed, (c) which feedback phrase or trace
observation drove the change. The storage layer
(`skill_refinements`) holds both `original_prompt` and
`proposed_prompt` so the UI can compute the diff at render time.

If — and only if — an edit would NOT improve estimates on this
kind of input, the refiner returns the original prompt verbatim
and an explanation that says it intentionally proposed nothing
and why. Vagueness alone is never a reason to disengage.
