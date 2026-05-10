-- 026_visual_layer.sql
--
-- Phase 6: visual layer. Two LLM-backed analytical tools land here so
-- the Phase 7 reasoning agent can filter and rank cohorts by plain-
-- language fit and visual appearance, not just numeric attributes:
--
--   1. summarize_listing      — structured Czech-real-estate Claude
--                                summary of a single listing snapshot.
--   2. compare_listing_images — pairwise Claude-vision comparison
--                                between two listings, scored across
--                                six fixed tenant-relevant dimensions.
--
-- This migration adds:
--
--   1. listing_summaries: cache table keyed on (sreality_id,
--      snapshot_id). Auto-invalidates when a new snapshot is recorded
--      (because it's a new id), preserving the Phase 7 audit story:
--      "this estimation used the summary tied to that exact snapshot."
--
--   2. listing_image_comparisons: cache table keyed on a canonical-
--      ordered pair (sreality_id_a < sreality_id_b). Repeat calls
--      return instantly; the agent will hit this hard.
--
--   3. llm_calls.called_for CHECK constraint extended to include
--      'compare_listing_images' (the existing 'summarize_listing'
--      enum value already lives in migration 020 — no change needed
--      for the summary tool's audit row).
--
--   4. Four app_settings rows: a system prompt + model name pair for
--      each tool. Operators can edit prompts via the Settings UI
--      without a redeploy; every prior value is preserved by the
--      app_settings_history trigger from migration 020.
--
-- RLS enabled on the new tables; no policies. Frontend reaches these
-- through the bearer-token-gated FastAPI service, mirroring the
-- estimation_runs / parsed_url_cache / llm_calls pattern.

------------------------------------------------------------------
-- 1. listing_summaries
------------------------------------------------------------------

create table listing_summaries (
  id            bigserial primary key,
  sreality_id   bigint not null references listings(sreality_id) on delete cascade,
  snapshot_id   bigint not null references listing_snapshots(id) on delete cascade,
  summary       jsonb  not null,
  model         text   not null,
  llm_call_id   bigint references llm_calls(id) on delete set null,
  cost_usd      numeric(10, 6),
  created_at    timestamptz not null default now(),
  unique (sreality_id, snapshot_id)
);

create index on listing_summaries (sreality_id, created_at desc);

alter table listing_summaries enable row level security;

------------------------------------------------------------------
-- 2. listing_image_comparisons
------------------------------------------------------------------

create table listing_image_comparisons (
  id              bigserial primary key,
  sreality_id_a   bigint not null references listings(sreality_id) on delete cascade,
  sreality_id_b   bigint not null references listings(sreality_id) on delete cascade,
  comparison      jsonb  not null,
  n_images_a      integer not null,
  n_images_b      integer not null,
  model           text   not null,
  llm_call_id     bigint references llm_calls(id) on delete set null,
  cost_usd        numeric(10, 6),
  created_at      timestamptz not null default now(),
  check (sreality_id_a < sreality_id_b),
  unique (sreality_id_a, sreality_id_b)
);

create index on listing_image_comparisons (sreality_id_a);
create index on listing_image_comparisons (sreality_id_b);

alter table listing_image_comparisons enable row level security;

------------------------------------------------------------------
-- 3. llm_calls.called_for: add 'compare_listing_images'
------------------------------------------------------------------

alter table llm_calls
  drop constraint llm_calls_called_for_check,
  add constraint llm_calls_called_for_check
    check (called_for in (
      'parse_url', 'summarize_listing', 'compare_listing_images'
    ));

------------------------------------------------------------------
-- 4. app_settings seeds for the visual layer
------------------------------------------------------------------

insert into app_settings (key, value, description, updated_by) values
  (
    'llm_summary_system_prompt',
    to_jsonb($PROMPT$You produce a short, structured summary of a single Czech real-estate listing.

You will be given the listing's structured fields (area, disposition, price,
locality, condition, building_type, energy_rating, balcony/lift/parking flags)
and its free-text description (Czech; do not translate). Use ONLY information
present in the input — do not infer from outside knowledge or invent claims.

Czech real-estate vocabulary you may rely on:
- Disposition codes (1+kk, 2+1, ...) describe room layout. "+kk" = kitchenette
  in the living room; "+1" = separate kitchen.
- "Po rekonstrukci" = recently renovated. "Před rekonstrukcí" = needs work.
  "Novostavba" = new build. "Velmi dobrý stav" = very good condition.
- "Cihla" = brick (warmer, quieter, preferred). "Panel" = prefab concrete
  (cheaper, less insulated). "Skelet" = concrete frame.
- "Energetická náročnost" A-G: A is best, G worst.

You MUST call the `record_listing_summary` tool exactly once with these fields:

- headline: one short sentence (max 120 chars) capturing the listing's
  identity. Example: "Renovated 2+kk in Vinohrady with balcony and parking."
  Czech or English — match the description's language.

- key_highlights: 2-5 short strings (each max 80 chars) describing what makes
  the listing attractive. Strict facts only: "south-facing balcony", "newly
  renovated kitchen", "elevator + secure parking", "panoramic view of Petřín".
  Do NOT invent qualities not stated.

- concerns: 0-5 short strings describing factual drawbacks evident from the
  input. Examples: "ground floor (street-facing)", "no balcony", "panel
  building from 1970s", "energy rating G". If none are evident, return [].
  Do NOT moralise; report what the seller didn't say only when its absence
  is itself notable (e.g., no parking mentioned in a category that usually has it).

- condition_assessment: one of "excellent" | "good" | "average" | "poor"
  | "unknown". Maps from the listing's stated condition + description tone:
    excellent = novostavba / po rekonstrukci with strong language
    good      = velmi dobrý stav / dobrý stav
    average   = before-renovation listings, mixed signals
    poor      = před rekonstrukcí / k demolici
    unknown   = condition field empty AND description silent on condition

- target_audience: one of "family" | "couple" | "single_professional"
  | "investor" | "student" | "general". Pick the most likely fit based on
  size, layout, and locality cues. Use "general" if no cue is strong.

Output ONLY the tool call. No prose outside the tool call.$PROMPT$::text),
    'System prompt sent to Claude when summarizing a listing for the toolkit. Editing this changes summarize_listing behaviour for the next cache miss. The trigger on app_settings preserves every prior version in app_settings_history.',
    'seed'
  ),
  (
    'llm_summary_model',
    '"claude-sonnet-4-5"'::jsonb,
    'Anthropic model ID used by toolkit.summaries.summarize_listing. Override via the Settings UI; Sonnet is the right cost/quality target for short structured summaries.',
    'seed'
  ),
  (
    'llm_image_compare_system_prompt',
    to_jsonb($PROMPT$You compare the photos of two Czech real-estate listings (Listing A and
Listing B) along six fixed tenant-relevant dimensions. The user will send
you each listing's images in order, then ask you to compare.

For EACH of the six dimensions below you return:
- score: a number in [0.0, 1.0] (1.0 = visually indistinguishable on this
  dimension; 0.0 = completely different). null if `observed=false`.
- observed: true if the relevant subject is visible in BOTH listings'
  images. false if at least one side has no image showing this subject.
- reasoning: 1-2 sentences citing what you actually see. Reference specific
  visual features (cabinet style, window proportion, flooring material).
  No tenure-of-listing assumptions; describe images, not narratives.

Dimensions (apply strictly):

1. exterior — the building from the outside. Façade material (panel /
   brick / stucco), age, balcony shape, entrance, surrounding street.
   Score by overall built-form similarity, not view.

2. kitchen — equipment level and standard. Cabinet finish (laminate vs
   real wood vs lacquer), countertop material (laminate / stone), built-in
   appliances, layout (galley / L / island), backsplash. NOT about
   colour palette unless extreme.

3. windows_and_light — window size, proportion, and how much daylight
   reaches the rooms. Floor-to-ceiling vs standard, single vs double
   exposure, brightness of interior shots.

4. floor_finish — the floor surface. Wood plank vs laminate vs vinyl
   vs tile vs concrete. Plank width, finish (matte/glossy), colour
   tone. Score by category match first, finish second.

5. lighting — light fixtures (chandeliers, pendants, recessed,
   industrial). Style and quality, not brightness.

6. styling — overall visual coherence. "decent" listings: consistent
   palette, intentional choices, staged or lived-in but cared for.
   "out of line" listings: clashing finishes, dated fixtures, unkempt
   rooms, obvious staging mismatches. Score by similarity of the
   overall impression, not absolute quality.

After scoring all six, compute:
- overall_similarity: the unweighted mean of the OBSERVED dimension
  scores. If zero dimensions are observed, return 0.0.
- summary: 1-2 sentences naming the strongest match and the strongest
  divergence.

You MUST call the `record_image_comparison` tool exactly once with all
six dimensions plus overall_similarity and summary. Do not output any
text outside the tool call.$PROMPT$::text),
    'System prompt sent to Claude vision when comparing the images of two listings. Editing this changes compare_listing_images behaviour for the next cache miss. The trigger on app_settings preserves every prior version in app_settings_history.',
    'seed'
  ),
  (
    'llm_image_compare_model',
    '"claude-sonnet-4-5"'::jsonb,
    'Anthropic model ID used by toolkit.image_similarity.compare_listing_images. Vision is materially more expensive than text; check llm_calls.cost_usd before bumping to a more capable tier.',
    'seed'
  );
