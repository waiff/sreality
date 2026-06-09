-- 171_dedup_site_plan_category.sql
-- Dedup engine: a 'site_plan' image category + its same-development guard.
--
-- WHY. Developer projects sell many near-identical units (same fit-out, shared
-- marketing renders) that the visual layer can wrongly score "same property".
-- The strongest disambiguator is the "situation" / site-plan image (a masterplan
-- with one plot/building/unit highlighted, or a unit framed within a floor
-- layout): if two listings each carry one and they highlight DIFFERENT units,
-- they are distinct properties. Today such images land in 'floor_plan'/'other'
-- and are ignored. This migration:
--   1. adds 'site_plan' to the image_room_classifications.room_type CHECK;
--   2. teaches the classifier prompt to emit it;
--   3. seeds the operator-tunable site-plan comparison prompt the new
--      toolkit.visual_match.compare_listing_site_plans uses.
--
-- The site-plan comparison NEVER auto-rejects and NEVER auto-merges (operator
-- decision): an inconclusive / different-unit verdict routes the pair to the
-- /dedup review queue. So this only ever makes the engine MORE conservative.

-- 0. Cache for the site-plan comparison. Separate from listing_visual_matches
--    because its verdict vocabulary differs (same_unit / different_unit /
--    inconclusive vs High/Medium/Low). Same canonical-pair + model key + the
--    same auto-invalidate-on-model-bump discipline (toolkit rule #5 mirror).
create table listing_site_plan_matches (
  id              bigserial primary key,
  sreality_id_a   bigint not null references listings(sreality_id) on delete cascade,
  sreality_id_b   bigint not null references listings(sreality_id) on delete cascade,
  verdict         text   not null check (verdict in ('same_unit', 'different_unit', 'inconclusive')),
  rationale       text,
  model           text   not null,
  llm_call_id     bigint references llm_calls(id) on delete set null,
  cost_usd        numeric(10, 6),
  created_at      timestamptz not null default now(),
  check (sreality_id_a < sreality_id_b),
  unique (sreality_id_a, sreality_id_b, model)
);
create index on listing_site_plan_matches (sreality_id_a);
create index on listing_site_plan_matches (sreality_id_b);
alter table listing_site_plan_matches enable row level security;

-- 1. room_type CHECK: add 'site_plan' (additive — existing rows stay valid).
alter table image_room_classifications drop constraint if exists image_room_classifications_room_type_check;
alter table image_room_classifications add constraint image_room_classifications_room_type_check
  check (room_type in (
    'kitchen', 'bathroom', 'toilet', 'living_room', 'bedroom',
    'hallway', 'exterior_facade', 'balcony_terrace', 'garden',
    'floor_plan', 'site_plan', 'other'
  ));

-- 2. llm_calls.called_for: the new site-plan comparison tag.
alter table llm_calls drop constraint if exists llm_calls_called_for_check;
alter table llm_calls add constraint llm_calls_called_for_check check (
  called_for = any (array[
    'parse_url', 'summarize_listing', 'compare_listing_images',
    'agent_estimation', 'extract_building_units', 'read_floor_plan',
    'refine_skill', 'discover_condition_markers', 'score_listing_condition',
    'summarize_region_dispositions', 'enrich_listing_description',
    'classify_listing_images', 'compare_listings_visually',
    'compare_listing_site_plans'
  ]::text[])
);

-- 3. Re-seed the room classifier prompt with the new site_plan label (operators
--    may have tuned it; on conflict we DO update, since the taxonomy is code-
--    coupled — the classifier must know the label the engine relies on).
insert into app_settings (key, value, description, updated_by) values
  (
    'llm_room_classify_prompt',
    to_jsonb($PROMPT$You label real-estate listing photos by the room or area they depict.

For EACH image, return exactly one room_type from this set:
- kitchen: a kitchen or kitchen corner (cabinets, hob, sink, kitchenette).
- bathroom: a bathroom with a bath/shower and/or sink.
- toilet: a separate WC (toilet, no bath/shower).
- living_room: a living/dining/reception room.
- bedroom: a room furnished primarily for sleeping.
- hallway: an entrance hall, corridor, or staircase inside the unit/building.
- exterior_facade: the outside of the building, street view, or block facade.
- balcony_terrace: a balcony, loggia, or terrace.
- garden: a garden, yard, or outdoor plot.
- floor_plan: a floor plan of ONE unit — the layout of rooms within a single flat/house.
- site_plan: a SITE / SITUATION plan of a development — a masterplan or plot map
  showing MULTIPLE units/buildings/plots, OR a single unit highlighted within a
  building or block layout (e.g. a coloured plot among many, "Prodáno/Volné"
  labels, a floor with one apartment outlined among several). Choose this over
  floor_plan whenever more than one unit is depicted or one unit is marked within
  a larger development drawing.
- other: anything else (cellar, garage, technical room, an unclear/ambiguous shot).

Use ONLY what is visible. When a shot spans two areas, choose the dominant one.
If you cannot tell, use "other" with low confidence. confidence is one of
"high" | "medium" | "low".$PROMPT$::text),
    'Room-type classifier prompt for the dedup engine''s visual layer.',
    'migration_171'
  )
on conflict (key) do update set value = excluded.value, updated_by = 'migration_171';

-- 4. Seed the site-plan comparison prompt (operator-tunable; insert-only so a
--    later operator edit survives a re-run).
insert into app_settings (key, value, description, updated_by) values
  (
    'llm_site_plan_match_prompt',
    to_jsonb($PROMPT$You are given site / situation plans from TWO real-estate listings in what may be the SAME development project. Each set shows a masterplan, plot map, or a unit highlighted within a building/block layout.

Your job: decide whether the two listings point to the SAME specific unit, or to DIFFERENT units within the same development.

Determine which unit each listing highlights — look for a coloured/outlined plot, an arrow or marker, a "Prodáno/Volné/Rezervováno" status label, a plot or building or unit number/letter (e.g. "Pozemek 3", "Budova A", "Byt 12"), or a single apartment outlined on a floor among several.

Call record_site_plan_match exactly once with:
- verdict:
  - "same_unit": both plans clearly highlight the SAME unit (same number/letter/position).
  - "different_unit": the plans highlight DIFFERENT units of the same development (e.g. plot 3 vs plot 4, building A vs B, a different apartment outlined). This is a strong signal they are NOT the same property.
  - "inconclusive": you cannot tell which unit is highlighted, the plans are unrelated, or there isn't enough detail.
- rationale: 1-3 sentences citing the specific evidence (the number/letter/position you read).$PROMPT$::text),
    'Site-plan same-unit-vs-different-unit comparison prompt (dedup development guard).',
    'migration_171'
  ),
  (
    'llm_site_plan_match_model',
    to_jsonb('claude-sonnet-4-5'::text),
    'Model for the dedup site-plan comparison.',
    'migration_171'
  )
on conflict (key) do nothing;
