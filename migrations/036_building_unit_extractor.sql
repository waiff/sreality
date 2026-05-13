-- 036_building_unit_extractor.sql
--
-- Phase B1: URL ingest + agent unit proposal for the building-decomposition
-- track. Migration 035 laid the persistence foundation (building_runs);
-- this migration adds the cache + skill + operator-tunable settings the
-- extractor needs.
--
-- Four things land here:
--
--   1. building_unit_extractions: cache table keyed on
--      (sreality_id, snapshot_id). Same shape and rationale as
--      listing_summaries (migration 027) — a new snapshot of the
--      building auto-invalidates by virtue of the PK including the
--      snapshot id, preserving the audit story "this proposal was
--      derived from that exact snapshot." Write-allowed exception
--      per CLAUDE.md toolkit rule #5 (LLM is the source of truth,
--      we cache locally to keep repeats fast and Anthropic-friendly).
--
--   2. llm_calls.called_for CHECK constraint extended with
--      'extract_building_units' so the audit trail tags vision
--      calls for building decomposition consistently with the
--      other LLM-backed tools.
--
--   3. Four app_settings rows:
--      - llm_building_extractor_system_prompt: the canonical prompt
--        body, mirrored on-disk at
--        skills/building_unit_extractor_v1/SKILL.md. Operators edit
--        the live value via the Settings UI; every prior version is
--        preserved in app_settings_history (migration 020 trigger).
--      - llm_building_extractor_model: Anthropic model id.
--        Sonnet 4.5 is the right cost/quality target for vision-led
--        structural extraction; bump only after checking llm_calls.cost_usd.
--      - llm_building_extractor_max_images: cap on the number of
--        R2-stored images base64-encoded into the vision payload.
--        Default 8 covers hero + floor plans + interior on a typical
--        sreality `dum` listing without ballooning tokens.
--      - building_default_estimator_skill: which apartment-estimator
--        skill the B2 orchestrator passes to each per-unit child
--        estimation_runs row. Default 'rental_estimator_v1'. This
--        is THE knob that makes apartment estimations inside
--        buildings consistent with standalone apartment
--        estimations — see ROADMAP.md "Phase B2" → orchestrator
--        step and CLAUDE.md architectural rule #13 + toolkit rule #5.
--
-- RLS enabled on the new cache table; no policies. The frontend
-- never reads building_unit_extractions directly — the unit proposal
-- lives on `building_runs.units_proposal` after the extractor runs,
-- which is reachable through GET /buildings/{id}.
--
-- A `skills` row is intentionally NOT seeded for the extractor: B1
-- is a single-shot vision call, not an iterative tool-use loop, so
-- `skills.limits` (max_iterations / max_cost_usd / wall_clock_timeout_s)
-- has no runtime meaning here. Prompt + model + image cap all live in
-- app_settings instead. The on-disk skills/building_unit_extractor_v1/
-- SKILL.md is documentation only — it captures the extractor's
-- contract for future readers without binding any runtime behaviour.
-- If a later phase makes extraction agent-driven we add the skills
-- row then and migrate the prompt out of app_settings.

------------------------------------------------------------------
-- 1. building_unit_extractions
------------------------------------------------------------------

create table building_unit_extractions (
  id            bigserial primary key,
  sreality_id   bigint not null references listings(sreality_id) on delete cascade,
  snapshot_id   bigint not null references listing_snapshots(id) on delete cascade,
  units         jsonb  not null,
  building      jsonb  not null,
  confidence    text   not null,
  warnings      jsonb,
  n_images      integer not null default 0,
  model         text   not null,
  llm_call_id   bigint references llm_calls(id) on delete set null,
  cost_usd      numeric(10, 6),
  created_at    timestamptz not null default now(),
  unique (sreality_id, snapshot_id)
);

create index on building_unit_extractions (sreality_id, created_at desc);

alter table building_unit_extractions enable row level security;

------------------------------------------------------------------
-- 2. llm_calls.called_for: add 'extract_building_units'
------------------------------------------------------------------

alter table llm_calls
  drop constraint llm_calls_called_for_check,
  add constraint llm_calls_called_for_check
    check (called_for in (
      'parse_url', 'summarize_listing', 'compare_listing_images',
      'agent_estimation', 'extract_building_units'
    ));

------------------------------------------------------------------
-- 3. app_settings seeds for the building extractor
------------------------------------------------------------------

insert into app_settings (key, value, description, updated_by) values
  (
    'llm_building_extractor_system_prompt',
    to_jsonb($PROMPT$You decompose a single Czech multi-unit building listing (typically a
`rodinný dům` / `činžovní dům` on sreality.cz or equivalent) into the
apartment units it contains.

You will be given:
- The listing's structured fields (total area, year built, locality,
  category, condition, ownership).
- Its free-text description (Czech; do not translate).
- Its photos and floor plans in order. Floor plans usually show unit
  layouts; interior photos show condition; exterior photos show the
  building shell.

Use ONLY information present in the input — never infer counts or
floors that the text and images do not support together.

Czech real-estate vocabulary you may rely on:
- "bytová jednotka" / "bj" = apartment unit. "garsoniéra" / "1+kk" =
  studio. "+kk" = kitchenette in the living room; "+1" = separate
  kitchen.
- "podlaží" / "patro" = storey. "přízemí" / "1. NP" = ground floor.
  "podkroví" = attic; "nezkolaudované podkroví" = unconverted attic
  (set `is_potential=true`).
- "Po rekonstrukci" = recently renovated. "Před rekonstrukcí" =
  needs work. "Novostavba" = new build. "Skelet" = concrete frame.
  "Cihla" = brick. "Panel" = prefab concrete.

You MUST call `record_building_units` exactly once with these fields:

- units: array of unit objects. One entry per discrete apartment unit
  visible in the floor plans / description, INCLUDING potential units
  (e.g. an unconverted attic that could become a flat). Each entry:
    * unit_id: stable string, "u1" / "u2" / "u3" in display order
      (ground floor first, attic last).
    * label: short human-readable label such as "1st floor flat" or
      "attic" — Czech or English, match the source language.
    * floor: short string. "ground" / "1" / "2" / "attic" /
      "basement". Use the source's notation when it is explicit.
    * area_m2: usable area of the unit in m², a number. If only a
      total building area is given, distribute proportionally and
      add a warning.
    * disposition: Czech code such as "2+kk" / "3+1" / "garsoniéra".
      Null if the source is silent and the floor plan does not show
      enough to infer.
    * condition: one of "novostavba" | "po_rekonstrukci" |
      "velmi_dobry" | "dobry" | "pred_rekonstrukci" | "k_demolici"
      | "unknown". Map from the photos for that unit when the text
      is silent.
    * is_potential: true ONLY for units that do not exist yet but
      could be built (unconverted attic, dividable large flat).
      false for every currently-habitable unit.
    * source: "description" | "floor_plan" | "both" — which input
      grounded the entry. "user_added" is reserved for the operator
      confirmation step.
    * notes: 0-1 short sentences (max 200 chars) noting anything
      relevant: balcony, garden access, separate entrance, shared
      utilities. Empty string if nothing notable.

- building: a single object summarising the building itself:
    * floor_count: integer count of above-ground storeys (excluding
      attic). null if not stated and not clear from photos.
    * has_attic: true / false / null.
    * year_built: integer (CE) if stated; null otherwise.
    * construction_type: one of "cihla" | "panel" | "skelet" |
      "drevostavba" | "smiseny" | "unknown".
    * total_area_m2: total usable area summed across declared units,
      a number. null if no per-unit areas could be assigned.
    * condition: one of "novostavba" | "po_rekonstrukci" |
      "velmi_dobry" | "dobry" | "pred_rekonstrukci" | "k_demolici"
      | "unknown" — overall building condition, NOT a per-unit value.
    * notes: 0-1 short sentences (max 200 chars).

- confidence: "high" | "medium" | "low":
    high   = description names every unit AND floor plans align;
    medium = description names a count but unit boundaries inferred
             mostly from floor plans, OR vice versa;
    low    = many gaps — count inferred from photos only, areas
             distributed proportionally, condition guessed.

- warnings: 0-5 short strings noting anything the operator should
  double-check on review. Examples: "two units share a single
  bathroom on floor 1", "attic shown but no dimensions given",
  "stated 4 flats but only 3 floor plans".

Output ONLY the tool call. No prose outside the tool call.$PROMPT$::text),
    'System prompt sent to Claude vision when decomposing a building listing into apartment units. Editing this changes extract_building_units behaviour for the next cache miss. The trigger on app_settings preserves every prior version in app_settings_history.',
    'seed'
  ),
  (
    'llm_building_extractor_model',
    '"claude-sonnet-4-5"'::jsonb,
    'Anthropic model id used by toolkit.building_extraction.extract_building_units. Vision is materially more expensive than text; check llm_calls.cost_usd before bumping.',
    'seed'
  ),
  (
    'llm_building_extractor_max_images',
    '8'::jsonb,
    'Maximum number of R2-stored images base64-encoded into the building-extractor vision payload. Default 8 covers hero + floor plans + interior on a typical dum listing without ballooning tokens.',
    'seed'
  ),
  (
    'building_default_estimator_skill',
    '"rental_estimator_v1"'::jsonb,
    'The skill name the B2 orchestrator passes to each per-unit child estimation_runs row. Default rental_estimator_v1 so apartment estimations inside buildings stay consistent with standalone apartment estimations. Change via Settings UI to switch the building flow to rental_estimator_full_v1 or a future skill — both surfaces will benefit from the upgrade in lockstep.',
    'seed'
  );
