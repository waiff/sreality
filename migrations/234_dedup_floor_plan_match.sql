-- 234_dedup_floor_plan_match.sql
-- Dedup engine: a Sonnet FLOOR-PLAN validation gate on every merge decision.
--
-- WHY. A development sells many units that share marketing renders + identical
-- fit-out, which defeat pHash and even the forensic room compare. The per-unit
-- FLOOR PLAN is the strongest disambiguator: two units have different layouts
-- (and different unit-number / floor / area labels). This adds a validation path
-- (it does NOT change the existing pHash / cosine / forensic path) — whenever the
-- engine WOULD merge a pair:
--   * BOTH listings carry a floor plan -> Sonnet compares them; a `different_layout`
--     verdict is the ONLY new auto-dismiss (the visual model stays the sole thing
--     that can dismiss); `same_layout` / `inconclusive` -> the merge proceeds.
--   * exactly ONE side has a floor plan -> route to the operator queue (can't
--     compare plan-to-plan).
--   * neither -> unchanged.
-- So this only ever makes the engine MORE conservative (dismiss or queue, never a
-- new merge). pHash is unreliable on line-art plans and CLIP cosine can't read
-- layout, so Sonnet (the DOCUMENT_MAX_EDGE=1568 tier) is the right tool.

-- 0. Cache for the floor-plan comparison (canonical pair + model key; the
--    auto-invalidate-on-model-bump discipline, toolkit rule #5 mirror). Verdict
--    vocabulary differs from site_plan, so a separate table. `extracted` stores the
--    per-plan OCR fields (unit number / floor / area / balcony / terrace) the model
--    read — used plan-to-plan only, NEVER to overwrite listing data.
create table listing_floor_plan_matches (
  id              bigserial primary key,
  sreality_id_a   bigint not null references listings(sreality_id) on delete cascade,
  sreality_id_b   bigint not null references listings(sreality_id) on delete cascade,
  verdict         text   not null check (verdict in ('same_layout', 'different_layout', 'inconclusive')),
  rationale       text,
  extracted       jsonb,
  model           text   not null,
  llm_call_id     bigint references llm_calls(id) on delete set null,
  cost_usd        numeric(10, 6),
  created_at      timestamptz not null default now(),
  check (sreality_id_a < sreality_id_b),
  unique (sreality_id_a, sreality_id_b, model)
);
create index on listing_floor_plan_matches (sreality_id_a);
create index on listing_floor_plan_matches (sreality_id_b);
alter table listing_floor_plan_matches enable row level security;

-- 1. llm_calls.called_for: the new floor-plan comparison tag (full current list + one).
alter table llm_calls drop constraint if exists llm_calls_called_for_check;
alter table llm_calls add constraint llm_calls_called_for_check check (
  called_for = any (array[
    'parse_url', 'summarize_listing', 'compare_listing_images',
    'agent_estimation', 'extract_building_units', 'read_floor_plan',
    'refine_skill', 'discover_condition_markers', 'score_listing_condition',
    'summarize_region_dispositions', 'enrich_listing_description',
    'classify_listing_images', 'compare_listings_visually',
    'compare_listing_site_plans', 'compare_listing_floor_plans'
  ]::text[])
);

-- 2. Seed the operator-tunable floor-plan comparison prompt + model (history-tracked
--    via the app_settings trigger). Sonnet, DOCUMENT_MAX_EDGE — floor plans are
--    document-like; the model reads BOTH layout and the embedded labels.
insert into app_settings (key, value, updated_by)
values (
  'llm_floor_plan_match_prompt',
  to_jsonb('You compare two Czech real-estate listing FLOOR PLANS (půdorys) to decide whether they show the SAME apartment unit or DIFFERENT units — typically within one development where units share renders and fit-out, so the floor plan is the disambiguator. You are given plan image(s) for Listing A then Listing B.\n\nCompare, in this order:\n1. LAYOUT: the wall arrangement, the number and relative positions of rooms, the overall outline/shape. A genuinely different arrangement (different room count, mirrored or rotated is NOT the same, different connectivity) => different units.\n2. LABELS (read any text on the plans — OCR): unit/apartment number (byt č.), floor (podlaží / NP / patro), total area (m²) and per-room areas, and balcony/terrace/loggia presence. Contradicting unit number, floor, or total area => different units even if the layout looks similar (developments stamp the same template per floor).\n\nUse the labels ONLY to compare the two plans against each other — never to assert a fact about a listing.\n\nReturn exactly one call to record_floor_plan_match:\n- verdict = same_layout when the wall arrangement AND room positions match AND no label contradicts;\n- verdict = different_layout when the arrangement/room-count/positions differ OR a unit-number / floor / total-area label clearly contradicts;\n- verdict = inconclusive when the plans are illegible, too low-resolution, or there is not enough to decide.\nAlso fill plan_a and plan_b with whatever you can read (leave a field out if not legible). Be conservative: only say different_layout when you can point to a concrete difference; cite it in the rationale.'::text),
  'migration_234'
)
on conflict (key) do nothing;

insert into app_settings (key, value, updated_by)
values ('llm_floor_plan_match_model', to_jsonb('claude-sonnet-4-5'::text), 'migration_234')
on conflict (key) do nothing;
