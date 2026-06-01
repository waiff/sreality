-- 128_image_room_classifications.sql
-- Dedup engine rebuild (rule D, layer 2): per-image room-type classification.
-- The visual matcher pairs LIKE rooms (kitchen vs kitchen, bath vs bath, …)
-- before running the forensic same-property comparison, so it first needs to
-- know what each photo depicts. This is the cache for that classification.
--
-- Cache pattern mirrors listing_summaries / listing_image_comparisons
-- (migration 027): keyed on (image_id, model), auto-invalidates on a model
-- bump. The classifier is a write-allowed toolkit exception (CLAUDE.md toolkit
-- rule #5) — the LLM is the source of truth, this table is a mirror.
--
-- room_type taxonomy (the classifier is constrained to these). Interior types
-- carry the strongest same-flat signal; exterior_facade / floor_plan are
-- excluded from the pHash fast-path because whole developments reuse one
-- facade render / floor-plan image across distinct units.

create table image_room_classifications (
  id            bigserial primary key,
  image_id      bigint not null references images(id) on delete cascade,
  room_type     text   not null
                  check (room_type in (
                    'kitchen', 'bathroom', 'toilet', 'living_room', 'bedroom',
                    'hallway', 'exterior_facade', 'balcony_terrace', 'garden',
                    'floor_plan', 'other'
                  )),
  confidence    text   not null
                  check (confidence in ('high', 'medium', 'low')),
  model         text   not null,
  llm_call_id   bigint references llm_calls(id) on delete set null,
  cost_usd      numeric(10, 6),
  created_at    timestamptz not null default now(),
  unique (image_id, model)
);

create index on image_room_classifications (image_id);

alter table image_room_classifications enable row level security;

-- Audit tag for the room classifier's LLM calls.
alter table llm_calls drop constraint if exists llm_calls_called_for_check;
alter table llm_calls add constraint llm_calls_called_for_check check (
  called_for = any (array[
    'parse_url', 'summarize_listing', 'compare_listing_images',
    'agent_estimation', 'extract_building_units', 'read_floor_plan',
    'refine_skill', 'discover_condition_markers', 'score_listing_condition',
    'summarize_region_dispositions', 'enrich_listing_description',
    'classify_listing_images', 'compare_listings_visually'
  ]::text[])
);

-- Operator-tunable room-classifier prompt + model (app_settings_history trigger
-- from migration 020 preserves every prior value).
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
- floor_plan: a floor plan, schematic, or drawing (not a photograph).
- other: anything else (cellar, garage, technical room, an unclear/ambiguous shot).

Use ONLY what is visible. When a shot spans two areas, choose the dominant one.
If you cannot tell, use "other" with low confidence. confidence is one of
"high" | "medium" | "low".$PROMPT$::text),
    'Room-type classifier prompt for the dedup engine''s visual layer.',
    'migration_128'
  ),
  (
    'llm_room_classify_model',
    to_jsonb('claude-sonnet-4-5'::text),
    'Model for image room classification (dedup visual layer).',
    'migration_128'
  )
on conflict (key) do nothing;
