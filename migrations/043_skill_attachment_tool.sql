-- 043_skill_attachment_tool.sql
--
-- Wires the `read_floor_plan` tool (added by migration 042 + new
-- toolkit/floor_plan.py) into the estimator skills, and seeds two
-- new app_settings rows for the operator-tunable system prompt and
-- model. Same pattern as migration 027 (visual layer) /
-- migration 036 (building extractor).
--
-- The building_unit_extractor is intentionally NOT updated here —
-- it is not a `skills` row (see migration 036's header). The
-- extractor reads operator-supplied attachments + text directly
-- via the message content blocks assembled in
-- toolkit/building_extraction.py.

------------------------------------------------------------------
-- 1. app_settings seeds
------------------------------------------------------------------

insert into app_settings (key, value, description, updated_by) values
  (
    'llm_floorplan_system_prompt',
    to_jsonb($PROMPT$You analyse one operator-supplied image of a Czech residential
property — typically a floor plan, sometimes an interior or exterior
photo, sometimes a technical drawing the operator wants the estimation
agent to consider. Describe ONLY what is visible.

Czech vocabulary you may see and should preserve verbatim:
- "1+kk" / "2+kk" / "3+1" — disposition codes.
- "obývací pokoj" = living room, "ložnice" = bedroom, "kuchyně" =
  kitchen, "koupelna" = bathroom, "WC" = toilet, "balkón" / "lodžie"
  / "terasa" — outdoor spaces, "sklep" = cellar.
- "podlaží" / "patro" = floor, "přízemí" = ground floor,
  "podkroví" = attic.

You MUST call `record_floor_plan_analysis` exactly once with:
  - headline: short label (<= 120 chars) naming what the image shows.
  - image_kind: one of "floor_plan" | "photo_interior" |
    "photo_exterior" | "technical_drawing" | "other".
  - rooms: array of {label, area_m2, is_potential} entries for each
    labelled or clearly delimited room. area_m2 nullable when the
    drawing doesn't state it. is_potential is true only for spaces
    that aren't habitable yet (unconverted attic, basement). Empty
    array if image_kind != "floor_plan".
  - total_area_m2: number summing all visible room areas, or null
    when individual areas aren't given.
  - layout_text: 2-4 sentences of plain prose describing the
    layout. Mention room adjacencies, daylight orientation when
    visible, anything an estimator would care about.
  - confidence: "high" | "medium" | "low". high = floor plan with
    full room labels and dimensions; medium = partial labels or
    inferred areas; low = photo only, or hard-to-read drawing.

Output ONLY the tool call. No prose outside the tool call.$PROMPT$::text),
    'System prompt sent to Claude vision when reading one operator-supplied attachment via toolkit.floor_plan.read_floor_plan. Editing this changes behaviour for the next cache miss; every prior version is preserved in app_settings_history via the migration 020 trigger.',
    'seed'
  ),
  (
    'llm_floorplan_model',
    '"claude-sonnet-4-5"'::jsonb,
    'Anthropic model id used by toolkit.floor_plan.read_floor_plan. Vision is materially more expensive than text; check llm_calls.cost_usd before bumping.',
    'seed'
  );

------------------------------------------------------------------
-- 2. Skill allowed_tools updates
------------------------------------------------------------------

update skills
   set allowed_tools = allowed_tools || '["read_floor_plan"]'::jsonb,
       updated_by    = 'seed'
 where name in ('rental_estimator_v1', 'rental_estimator_full_v1')
   and not (allowed_tools @> '["read_floor_plan"]'::jsonb);
