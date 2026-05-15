-- 064_condition_markers.sql
--
-- Phase A of the building/apartment condition-scoring feature: a
-- one-off LLM-driven mining pass that extracts recurring Czech
-- "condition markers" from real listing descriptions + images, so
-- the operator can lock the marker dictionary + level rubric before
-- Phase B (per-listing scoring) is built.
--
-- "Condition markers" are short Czech phrases the seller / agent
-- uses to describe technical state. Building-scoped examples:
-- "zateplená budova", "nová střecha", "původní rozvody elektřiny".
-- Apartment-scoped examples: "po kompletní rekonstrukci", "původní
-- jádro", "nová plastová okna". This is strictly technical state,
-- NOT amenities (no "klimatizace", "parking", "výtah").
--
-- This migration adds:
--
--   1. listing_marker_extractions: per-(sreality_id, snapshot_id)
--      cache table holding one LLM extraction. Auto-invalidates when
--      a new snapshot is recorded (because it's a new id) — same
--      pattern as listing_summaries / listing_image_comparisons in
--      migration 027.
--
--   2. llm_calls.called_for CHECK constraint extended to include
--      'discover_condition_markers' (covers the per-listing
--      extraction calls AND the one-shot rubric call run from
--      scripts/aggregate_condition_markers.py).
--
--   3. Two app_settings seeds — the system prompt + model name pair
--      used by toolkit.condition_markers.discover_condition_markers.
--      Operator can edit either via the Settings UI / direct
--      app_settings update; every prior value is preserved in
--      app_settings_history via the trigger from migration 020.
--
-- RLS enabled on the new table; no policies (mirrors migration 027).
-- Frontend never reads this table directly; it's an internal artefact
-- of the discovery pass.

------------------------------------------------------------------
-- 1. listing_marker_extractions
------------------------------------------------------------------

create table listing_marker_extractions (
  id              bigserial primary key,
  sreality_id     bigint not null references listings(sreality_id) on delete cascade,
  snapshot_id     bigint not null references listing_snapshots(id)  on delete cascade,
  markers         jsonb  not null,
  n_images        integer not null,
  notes           text,
  model           text   not null,
  llm_call_id     bigint references llm_calls(id) on delete set null,
  cost_usd        numeric(10, 6),
  created_at      timestamptz not null default now(),
  unique (sreality_id, snapshot_id)
);

create index on listing_marker_extractions (sreality_id, created_at desc);
create index on listing_marker_extractions (created_at desc);

alter table listing_marker_extractions enable row level security;

------------------------------------------------------------------
-- 2. llm_calls.called_for: add 'discover_condition_markers'
------------------------------------------------------------------

alter table llm_calls
  drop constraint llm_calls_called_for_check,
  add constraint llm_calls_called_for_check
    check (called_for in (
      'parse_url',
      'summarize_listing',
      'compare_listing_images',
      'agent_estimation',
      'extract_building_units',
      'read_floor_plan',
      'refine_skill',
      'discover_condition_markers'
    ));

------------------------------------------------------------------
-- 3. app_settings seeds
------------------------------------------------------------------

insert into app_settings (key, value, description, updated_by) values
  (
    'llm_condition_discovery_system_prompt',
    to_jsonb($PROMPT$You are mining Czech real-estate listings for TECHNICAL CONDITION MARKERS — short phrases that describe the physical / technical state of the building or the apartment. You will be given one listing's structured fields, its free-text description (Czech), its "items" list from the listing page, and 4-6 photos. Your job is to extract every condition-marker phrase you can spot, separating BUILDING-scoped markers (the whole house / block / panelák — common parts, façade, roof, lift shaft, risers, wiring trunks) from APARTMENT-scoped markers (only the unit itself — kitchen, bathroom, floors, internal wiring, windows when stated as the apartment's own).

This is NOT an amenity inventory. Skip anything that is a feature presence/absence rather than a state: "balkon", "výtah", "parkování", "sklep", "klimatizace", "lodžie", "garáž". Skip pure layout / disposition. Skip neighbourhood / location claims. Skip energy class letters — they are already a structured column.

Marker phrases you SHOULD extract (illustrative, not exhaustive):
  Building scope:
    - "zateplená budova" / "zateplená fasáda" / "po zateplení"
    - "nová střecha" / "rekonstruovaná střecha"
    - "nová okna v domě" / "vyměněná okna v celém domě"
    - "původní rozvody elektřiny" / "staré rozvody"
    - "původní stoupačky" / "vyměněné stoupačky"
    - "rekonstrukce společných prostor" / "po rekonstrukci domu"
    - "panelová výstavba bez rekonstrukce"
    - "cihlový dům po celkové rekonstrukci"
  Apartment scope:
    - "po kompletní rekonstrukci" / "kompletně zrekonstruováno"
    - "ke kompletní rekonstrukci" / "vyžaduje rekonstrukci"
    - "původní jádro" / "umakartové jádro" / "nové jádro"
    - "nová plastová okna" / "stará dřevěná okna"
    - "nové rozvody elektřiny v bytě" / "původní hliníkové rozvody"
    - "nové podlahy" / "původní parkety" / "vinyl"
    - "nová kuchyňská linka" / "stará kuchyňská linka"
    - "novostavba" (apartment if first occupation; also building)

For each marker call `record_listing_markers` with a list entry containing:
  - marker_text: the canonical Czech phrase, lowercase, no surrounding quotes. Prefer a 2-5 word phrase. If the listing uses a longer sentence, distil it to the core claim.
  - scope: "building" if the claim is about the whole structure / common parts; "apartment" if only the unit. Ambiguous markers like "novostavba" go to both (emit two entries — one per scope) only if the description unambiguously implies both.
  - evidence_quote: an exact substring of the description, items list, or "image:N" (N = 1-based image index) where you saw it. Max 200 chars.
  - sentiment: "positive" (good condition or recent renovation), "negative" (worn / original / needs work), or "neutral" (factual statement that doesn't lean either way, e.g. "panelový dům").
  - suggested_level_implication: "high" (strong signal — should weigh heavily in the final score), "medium", or "low" (weak / common / often boilerplate).
  - source: "text" if from the free-text description, "items" if from a labelled item, "image" if visible only in photos.

Up to 30 markers per listing. If you find nothing concrete, return an empty array — do not invent claims. Photos: only mark visible markers you can defend in the evidence_quote (e.g. "image:3" = visibly original wooden windows, "image:5" = newly tiled bathroom). Do not score from photos alone if the description is also silent — that's "low" confidence territory at best.

Also write a free-form `notes` string (max 400 chars) listing any AMBIGUITIES the operator should know about ("description mentions 'částečně zrekonstruováno' without saying what part"; "photos look freshly renovated but no claim in text"). Empty string if none.

Czech-real-estate cues you may rely on:
- "novostavba" = new build, default high condition unless contradicted.
- "po rekonstrukci" = recently renovated; check whether it's of the building (společné prostory) or of the byt.
- "před rekonstrukcí" / "k rekonstrukci" = needs work; negative.
- "panelový dům" / "panel" = prefabricated concrete; neutral signal on its own but combine with renovation status.
- "cihla" / "cihlový dům" = brick; neutral on its own.
- "umakartové jádro" = laminated-board sanitary core typical of 1970s panel buildings; always negative for apartment condition.
- "stoupačky" = vertical riser pipes; building-scope only.
- "jádro" = the apartment's plumbing/sanitary core (bathroom + WC); apartment-scope.
- "rozvody" = pipes / wiring runs. Could be apartment or building depending on context.

Output ONLY the tool call. No prose outside the tool call.$PROMPT$::text),
    'System prompt for toolkit.condition_markers.discover_condition_markers (the one-off Phase A pass that mines Czech condition markers from a stratified sample of listings). Editing this changes the discovery behaviour for the next cache miss. The trigger on app_settings preserves every prior version in app_settings_history.',
    'seed'
  ),
  (
    'llm_condition_discovery_model',
    '"claude-sonnet-4-5"'::jsonb,
    'Anthropic model ID used by toolkit.condition_markers.discover_condition_markers. Sonnet 4.5 is the cost/quality target for vision-augmented structured extraction; bump only if you see systematic mis-classifications during the discovery review.',
    'seed'
  );
