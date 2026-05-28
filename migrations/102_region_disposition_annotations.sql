-- 102_region_disposition_annotations.sql
--
-- summarize-1 (Summarize track): a one-to-two-sentence natural-language
-- annotation per per-disposition Kč/m² box plot in Browse > Stats.
-- Generated server-side by
-- toolkit.region_annotations.summarize_region_dispositions from the same
-- ppm2_box payload that already drives the DispositionBoxPlots component,
-- so the browser never holds an Anthropic key.
--
-- Same write-allowed cache pattern as listing_summaries (migration 027):
-- the LLM is the source of truth, this table is a mirror that
-- auto-invalidates. The cache key is (region_hash, day): a region's
-- annotations are generated once per calendar day so repeat browser
-- sessions don't re-bill the API. The next day's first view regenerates,
-- picking up the day's data drift.
--
-- This migration adds:
--   1. region_disposition_annotations: cache table keyed on
--      (region_hash, day). region_key is the deterministic serialization
--      of the active Browse filter set; region_hash is its sha256.
--   2. llm_calls.called_for: add 'summarize_region_dispositions'.
--   3. Two app_settings rows: system prompt + model for the annotator.
--      Operators tune both via the Settings UI without a redeploy; every
--      prior value is preserved by the app_settings_history trigger
--      (migration 020).
--
-- RLS enabled, no policies. Frontend reaches this through the bearer-
-- token-gated FastAPI service, mirroring listing_summaries.

------------------------------------------------------------------
-- 1. region_disposition_annotations
------------------------------------------------------------------

create table region_disposition_annotations (
  id            bigserial primary key,
  region_hash   text   not null,
  region_key    text   not null,
  day           date   not null default current_date,
  annotations   jsonb  not null,
  model         text   not null,
  llm_call_id   bigint references llm_calls(id) on delete set null,
  cost_usd      numeric(10, 6),
  created_at    timestamptz not null default now(),
  unique (region_hash, day)
);

create index on region_disposition_annotations (day);

alter table region_disposition_annotations enable row level security;

------------------------------------------------------------------
-- 2. llm_calls.called_for: add 'summarize_region_dispositions'
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
      'score_listing_condition',
      'summarize_region_dispositions'
    ));

------------------------------------------------------------------
-- 3. app_settings seeds for the region annotator
------------------------------------------------------------------

insert into app_settings (key, value, description, updated_by) values
  (
    'llm_region_annotation_system_prompt',
    to_jsonb($PROMPT$You annotate per-disposition price-per-m² (Kč/m²) box plots for a cohort of
Czech real-estate listings. The user gives you, for each disposition (1+kk,
2+kk, 3+1, ...), the five-number summary of its Kč/m² distribution plus the
listing count, and the cohort-wide price-per-m² percentiles for context.

Box-plot vocabulary:
- n = number of listings with both price and area in this disposition.
- min / max = lowest and highest Kč/m² observed.
- p25 / p75 = first and third quartiles; the box spans these (the IQR). A
  narrow box = tightly clustered prices; a wide box = dispersed prices.
- median = the middle value (drawn as the copper line).
- The chart draws Tukey 1.5×IQR whiskers clipped to [min, max]. A long
  upper whisker means a thin tail of expensive listings; a long lower
  whisker means a thin tail of cheap ones.

Czech real-estate context:
- Disposition codes (1+kk, 2+1, ...) describe room layout. "+kk" = kitchenette
  in the living room; "+1" = separate kitchen.
- Kč/m² is price per square metre. For rentals it is monthly Kč/m²; for sales
  it is the purchase Kč/m². Do not assume which — describe the numbers as given.

Write ONE annotation per disposition, each 1-2 sentences (max ~280 characters),
in clear English. Describe the SHAPE of that disposition's distribution:
- where prices cluster (median, box width / IQR),
- the spread (min-to-max),
- what the whiskers/tails reveal (a handful of high or low outliers),
- optionally how this disposition compares to the cohort or other dispositions
  (e.g. "below the cohort median", "the widest spread of any disposition").

STRICT RULES — these are factual descriptions, NOT advice:
- Report only what the numbers show. Do not invent reasons ("premium finish",
  "renovated") unless framed as a plausible read of a long tail, hedged
  ("likely reflects", "consistent with") — never as established fact.
- NEVER recommend a price, call anything cheap/expensive/overpriced/underpriced,
  a good deal, a bargain, good/poor value, or worth buying. No buy/sell/rent
  guidance. You describe distributions; you do not give opinions.
- Use the cohort's own currency unit (Kč/m²); round to whole numbers.
- Do not mention dispositions absent from the input.

You MUST call the `record_disposition_annotations` tool exactly once, with one
entry per disposition you were given. Output ONLY the tool call.$PROMPT$::text),
    'System prompt sent to Claude when annotating the per-disposition Kč/m² box plots in Browse > Stats (toolkit.region_annotations.summarize_region_dispositions). Editing this changes annotation behaviour for the next cache miss. The trigger on app_settings preserves every prior version in app_settings_history.',
    'seed'
  ),
  (
    'llm_region_annotation_model',
    '"claude-sonnet-4-5"'::jsonb,
    'Anthropic model ID used by toolkit.region_annotations.summarize_region_dispositions. Sonnet is the right cost/quality target for short distributional annotations; the per-region-per-day cache keeps spend bounded.',
    'seed'
  );
