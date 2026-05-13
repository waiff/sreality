---
name: building_unit_extractor_v1
description: Structural extractor that decomposes a Czech multi-unit building listing (dum / komercni) into apartment units. NOT an estimator.
runtime: app_settings
related_settings:
  - llm_building_extractor_system_prompt
  - llm_building_extractor_model
  - llm_building_extractor_max_images
  - building_default_estimator_skill
tool_envelope: record_building_units
---

# building_unit_extractor_v1 — documentation

Phase B1 of the building-decomposition track. This file is
**documentation only**. The runtime contract lives in three places:

1. `app_settings.llm_building_extractor_system_prompt` — the canonical
   prompt body the vision call uses. Editable via the Settings UI;
   `app_settings_history` (migration 020 trigger) preserves every
   prior version.
2. `app_settings.llm_building_extractor_model` — the Anthropic model
   id. Defaults to `claude-sonnet-4-5`.
3. `app_settings.llm_building_extractor_max_images` — cap on the
   number of R2-stored images base64-encoded into the vision payload.
   Defaults to 8.

A `skills` table row is intentionally NOT seeded for the extractor.
B1 is a single-shot vision call, not an iterative tool-use loop, so
`skills.limits` (max_iterations / max_cost_usd / wall_clock_timeout_s)
has no runtime meaning here. If a later phase makes extraction
agent-driven we'll add the skills row then and migrate the prompt
out of app_settings.

## What the extractor does

Reads a single multi-unit building listing's latest snapshot
(`raw_json.text` + `raw_json.items` + structured listings columns)
and up to `max_images` of its R2-stored photos. Sends them as a
single Claude vision call. The model MUST respond with one
`record_building_units` tool call carrying:

- `units[]` — one entry per apartment unit visible in the floor plans
  + description, including potential units (e.g. an unconverted
  attic, flagged with `is_potential=true`).
- `building` — single object summarising the building (storey count,
  year built, construction type, overall condition, etc.).
- `confidence` — `high` | `medium` | `low`.
- `warnings[]` — 0-5 short strings for things the operator should
  double-check.

If R2 is not configured or the listing has no R2-stored images yet,
the extractor falls back to description-only, stamps an extra
warning, and downgrades `confidence='high'` to `medium`. The
building flow is never blocked just because the image-download
phase hasn't caught up.

## Cache discipline

Results are cached in `building_unit_extractions` keyed on
`(sreality_id, snapshot_id)`. New snapshots auto-invalidate because
the PK includes the snapshot id — same pattern as `listing_summaries`.
`force_refresh=True` bypasses the cache.

## Relationship to the apartment estimator skill

This skill / setting bundle handles **structural extraction only** —
"what units does this building contain?". Per-unit rent / sale
estimation in Phase B2 uses the existing apartment estimator skill
(`rental_estimator_v1` by default, or `rental_estimator_full_v1` if
the operator switches), sourced from
`app_settings.building_default_estimator_skill`. That deliberate
reuse keeps apartment estimations inside a building consistent with
standalone apartment estimations — any improvement to the apartment
estimator skill rolls into the building flow automatically.

See ROADMAP.md "Building decomposition track" → Phase B2 →
"Reuse the existing apartment estimator skill" for the full
discussion.

## Operator playbook

- **Prompt edits:** Settings → `llm_building_extractor_system_prompt`.
  Effective on the next cache miss; existing cache entries are not
  invalidated (they remain pinned to whatever prompt produced them
  for audit purposes).
- **Model upgrades:** Settings → `llm_building_extractor_model`.
  Vision is materially more expensive than text — check
  `llm_calls.cost_usd` for `called_for='extract_building_units'`
  before bumping to a more capable tier.
- **Force re-extract:** `POST /buildings/{id}/re_extract` while the
  building is in `status='awaiting_input'`. Useful when a new
  snapshot lands between paste and operator confirmation, or when
  the prompt was updated after extraction.
- **Switching the per-unit estimator skill (B2):** Settings →
  `building_default_estimator_skill`. Affects every new building's
  per-unit children; runs already at `status='estimating'` use the
  skill that was active when their children were INSERTed.
