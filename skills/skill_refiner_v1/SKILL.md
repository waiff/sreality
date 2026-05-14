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

This is the Phase AI slice C skill. Migration 050 seeds the row;
runtime values live in the `skills` table.

Unlike the rental estimator, this skill does **not** drive the
agent loop in `api/agent.py`. The actual system prompt for the
refinement call comes from
`app_settings.llm_skill_refiner_system_prompt` (so the operator
can iterate on the refiner's behaviour via the Settings page
without redeploying), and the model id from
`app_settings.llm_skill_refiner_model`. The `skills` row exists so
`skill_refinements.skill_name` has a valid FK target and so the
refiner appears in the Settings inventory.

Operating principles (lifted from the live `app_settings` row;
keep in sync if you ever change either side):

1. Address the specific issue the operator raised — no more, no
   less.
2. Preserve every existing rule that isn't directly contradicted
   by the feedback.
3. Stay grounded in the trace. Disagree if the trace doesn't
   support the feedback.
4. Respect the tool whitelist. Tool whitelist edits are out of
   scope.
5. Keep the edit small. Massive restructures break A/B
   regressions.

The refiner returns the FULL new system_prompt (not a diff) plus
a 2-4 sentence explanation. The storage layer
(`skill_refinements`) holds both `original_prompt` and
`proposed_prompt` so the UI can compute the diff at render time.

If the feedback doesn't justify an edit, the refiner returns the
original prompt verbatim and an explanation that says it
intentionally proposed nothing. It never silently refuses.
