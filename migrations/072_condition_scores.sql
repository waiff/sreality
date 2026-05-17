-- 072_condition_scores.sql
--
-- Phase B of the building/apartment condition-scoring feature: the
-- per-listing scorer. Phase A produced the curated marker dictionary
-- and 5-level rubric (data/condition_markers_curated.json,
-- data/condition_rubric_v1.json). Phase B introduces:
--
--   1. toolkit.condition_scoring.score_listing_condition — a write-
--      allowed analytical tool that takes one listing's latest
--      snapshot and emits building_level / apartment_level (1..5)
--      with per-axis confidence and the marker IDs it relied on.
--   2. The two derived columns on `listings` so downstream filters
--      (find_comparables, /browse, frontend) can use them directly.
--
-- This migration adds:
--
--   1. listing_condition_scores: cache table keyed on
--      (sreality_id, snapshot_id), same auto-invalidation pattern
--      as listing_summaries / listing_image_comparisons (migration 027)
--      and listing_marker_extractions (migration 064). A new snapshot
--      = a new score row, recomputed on first call.
--
--   2. listings.building_condition_level + listings.apartment_condition_level
--      — both nullable integers in [1..5]. Populated by the scorer
--      inside the same transaction as the cache write, guarded so a
--      stale-snapshot scorer can't overwrite a fresher score.
--
--   3. llm_calls.called_for CHECK constraint extended to include
--      'score_listing_condition'.
--
--   4. Four app_settings rows that the scorer reads at call time:
--      - llm_condition_system_prompt (text) — operator-tunable.
--      - llm_condition_model (jsonb string) — anthropic model id.
--      - llm_condition_rubric (jsonb) — populated separately by
--        scripts/seed_condition_settings.py from
--        data/condition_rubric_v1.json. Empty {} until then.
--      - llm_condition_marker_dictionary (jsonb) — populated
--        separately from data/condition_markers_curated.json.
--        Empty {} until then.
--
--      The rubric + dictionary are kept out of this migration body
--      (they're ~225KB combined) so the migration stays readable.
--      The Python seed script reads the committed JSON files and
--      UPDATEs both app_settings rows; every prior version is
--      preserved by the app_settings_history trigger (migration 020).
--      Scorer raises a clear error if either is still empty at
--      call time.
--
-- RLS enabled on the new table; no policies. Frontend reaches the
-- two new listings columns via listings_public (extended in
-- migration 073).

------------------------------------------------------------------
-- 1. listing_condition_scores
------------------------------------------------------------------

create table listing_condition_scores (
  id                        bigserial primary key,
  sreality_id               bigint not null references listings(sreality_id) on delete cascade,
  snapshot_id               bigint not null references listing_snapshots(id) on delete cascade,
  building_level            integer,
  apartment_level           integer,
  building_markers_found    jsonb  not null default '[]'::jsonb,
  apartment_markers_found   jsonb  not null default '[]'::jsonb,
  building_confidence       numeric(4, 3),
  apartment_confidence      numeric(4, 3),
  notes                     text,
  n_images                  integer not null,
  model                     text   not null,
  llm_call_id               bigint references llm_calls(id) on delete set null,
  cost_usd                  numeric(10, 6),
  created_at                timestamptz not null default now(),
  unique (sreality_id, snapshot_id),
  check (building_level is null or building_level between 1 and 5),
  check (apartment_level is null or apartment_level between 1 and 5),
  check (building_confidence is null or building_confidence between 0.0 and 1.0),
  check (apartment_confidence is null or apartment_confidence between 0.0 and 1.0)
);

create index on listing_condition_scores (sreality_id, created_at desc);
create index on listing_condition_scores (created_at desc);

alter table listing_condition_scores enable row level security;

------------------------------------------------------------------
-- 2. listings.building_condition_level + apartment_condition_level
------------------------------------------------------------------

alter table listings
  add column building_condition_level  integer,
  add column apartment_condition_level integer;

alter table listings
  add constraint listings_building_condition_level_range
    check (building_condition_level is null or building_condition_level between 1 and 5),
  add constraint listings_apartment_condition_level_range
    check (apartment_condition_level is null or apartment_condition_level between 1 and 5);

create index on listings (building_condition_level);
create index on listings (apartment_condition_level);

------------------------------------------------------------------
-- 3. llm_calls.called_for: add 'score_listing_condition'
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
      'discover_condition_markers',
      'score_listing_condition'
    ));

------------------------------------------------------------------
-- 4. app_settings seeds
------------------------------------------------------------------

-- The system prompt is intentionally compact. The full rubric and
-- marker dictionary are injected at call time from the two jsonb
-- settings below, so this prompt only carries the invariant
-- contract (output shape, fallback chain, confidence policy,
-- output discipline). Editing this changes the next cache-miss
-- score; every prior version is preserved by app_settings_history.

insert into app_settings (key, value, description, updated_by) values
  (
    'llm_condition_system_prompt',
    to_jsonb($PROMPT$You score one Czech real-estate listing on two independent axes — BUILDING condition (the whole structure / common parts) and APARTMENT condition (the unit itself) — each on a 1..5 integer scale where 5 = excellent and 1 = critical.

You are given:
  * The listing's structured fields (area, disposition, price, locality, condition, building_type, energy_rating, year_built, balcony/lift/parking flags).
  * The free-text Czech description.
  * The page's items[] list (e.g. "Stav objektu: Po rekonstrukci", "Konstrukce budovy: Cihlová").
  * Optionally, 4-6 listing photos.
  * The curated marker dictionary (`<MARKER_DICTIONARY>` placeholder, injected at call time) — each entry has a stable `marker_id`, canonical Czech phrase, scope (building/apartment), sentiment, level_hint, and variant phrasings.
  * The level rubric (`<RUBRIC>` placeholder, injected at call time) — per-scope descriptions, required_marker_ids, disqualifying_marker_ids, confidence_policy.bands, and fallback_chain.

Your contract:

1. Detect which marker dictionary entries are SUPPORTED by this listing's evidence (description text, items list, or visible in photos). Use the entry's `variants` for fuzzy matching — exact wording isn't required, just an unambiguous claim. Emit each supported marker_id in `building_markers_found` (scope='building') or `apartment_markers_found` (scope='apartment').

2. Apply the rubric per scope:
   * If a `disqualifying_marker_id` for level N is present, the score CANNOT be N or higher.
   * If a `required_marker_id` for level N is present and no disqualifier blocks it, the score is AT LEAST N (consider level N+1 only if the level-N+1 description is also met).
   * If no markers are found in this scope, walk the rubric's `fallback_chain`:
        (a) Curated markers (none → next step)
        (b) listings.condition enum mapping: Novostavba→5, Po rekonstrukci→4, Velmi dobrý stav→4, Dobrý stav→3, Před rekonstrukcí→2, Špatný stav→1.
        (c) Weak structural signals (building_type, energy_rating, year_built) for building scope only.
        (d) Hard default: level=3, and assign confidence in the `silent_no_fallback` band (≤ 0.20).

3. Emit per-axis confidence (0..1) per the rubric's `confidence_policy.bands` — never exceed the upper bound of the band that fits the evidence.

4. Emit a short `notes` string (max 300 chars) ONLY when something non-obvious is happening: contradictions ('po rekonstrukci' + 'v původním stavu' on the same scope), photo-only signals, or the fallback path was used. Empty string otherwise.

You MUST call the `record_listing_condition` tool exactly once. Output ONLY the tool call. No prose outside the tool call.$PROMPT$::text),
    'System prompt for toolkit.condition_scoring.score_listing_condition. Injects <MARKER_DICTIONARY> and <RUBRIC> placeholders at call time. Editing this changes the scorer''s behaviour for the next cache miss. The trigger on app_settings preserves every prior version in app_settings_history.',
    'seed'
  ),
  (
    'llm_condition_model',
    '"claude-sonnet-4-5"'::jsonb,
    'Anthropic model ID used by toolkit.condition_scoring.score_listing_condition. Sonnet 4.5 is the cost/quality target; bump to Opus only if systematic misclassifications appear during backfill spot-checks.',
    'seed'
  ),
  (
    'llm_condition_rubric',
    '{}'::jsonb,
    'The 5-level rubric (data/condition_rubric_v1.json) injected verbatim into the scorer''s system prompt. Populated by scripts/seed_condition_settings.py after this migration is applied. score_listing_condition raises ScoringError if this is still empty at call time.',
    'seed'
  ),
  (
    'llm_condition_marker_dictionary',
    '{}'::jsonb,
    'The curated marker dictionary (data/condition_markers_curated.json) injected verbatim into the scorer''s system prompt. Populated by scripts/seed_condition_settings.py after this migration is applied. score_listing_condition raises ScoringError if this is still empty at call time.',
    'seed'
  );
