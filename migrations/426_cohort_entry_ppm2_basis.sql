-- 426_cohort_entry_ppm2_basis.sql
--
-- W5 of the per-m² measure-unification program
-- (docs/design/ppm2-measure-unification.md). After W4 (425), which created the
-- one named measure and its one named label.
--
-- W5 puts the Python and API call sites on that measure. Two of them need a
-- schema change; both are additive and neither rewrites a stored number.
--
-- ---------------------------------------------------------------------------
-- 1. estimation_cohort_entries.price_per_m2_basis
-- ---------------------------------------------------------------------------
-- `estimation_cohort_entries.price_per_m2` has been stored since migration 053
-- with NO record of what unit it was in. The column is written from the cohort
-- rows the agent saw, and those cohorts span sale, rental, auction and land --
-- so the same column holds monthly Kč/m² and capital Kč/m² side by side, and
-- nothing distinguishes them after the fact. This adds the label beside the
-- number, from the same row, in the same INSERT (api/agent.py
-- `_persist_cohort_entries`).
--
-- NULLABLE, and historical rows are LEFT NULL on purpose: NULL means "basis
-- unknown, pre-426", which is the truth. Backfilling it from today's
-- `listings.category_*` would be a guess about a row's state at estimation
-- time, and rules 8 and 12 both forbid it -- estimation_runs and everything
-- hanging off them are immutable audit, not live state. For the same reason
-- the stored `price_per_m2` values are NOT recomputed: a pre-426 entry keeps
-- the number the run actually used, floors and all, even where the measure
-- would now withhold it.
--
-- ---------------------------------------------------------------------------
-- 2. The region-annotator prompt learns that the unit is an INPUT
-- ---------------------------------------------------------------------------
-- Migration 104 seeded `llm_region_annotation_system_prompt` with the line
-- "Kč/m² is price per square metre. For rentals it is monthly Kč/m²; for sales
-- it is the purchase Kč/m². Do not assume which — describe the numbers as
-- given." That was the correct instruction while the caller could not say
-- which; it is the wrong one now that it can. The payload built by
-- toolkit.region_annotations._build_payload states the basis and its unit on
-- its own line, and a 'mixed' cohort never reaches the model at all -- it is
-- refused in Python, with no LLM call and no cache write, because a box plot
-- that stacks monthly rents on purchase prices has no describable shape.
--
-- Guarded on `updated_by = 'seed'` so an operator-customised prompt (Settings
-- page -> updated_by = 'settings_ui') is never clobbered. Verified against
-- production before writing this file: the row is still `seed`, untouched
-- since 2026-05-28, so the guard fires. The app_settings_history trigger
-- (migration 020) preserves the prior text either way.
--
-- Additive, no destructive step, no rebuild, no lock of consequence.

begin;

set local lock_timeout = '5s';

alter table estimation_cohort_entries
  add column if not exists price_per_m2_basis text;

comment on column estimation_cohort_entries.price_per_m2_basis is
  'The unit price_per_m2 on this row is in: one of measure_price_per_m2_basis''s '
  'four tokens (sale_capital_czk_m2 / rent_monthly_czk_m2 / land_capital_czk_m2, '
  'or NULL). Stamped from the cohort row at persist time by api/agent.py. NULL '
  'means "basis unknown, pre-426" and is NEVER backfilled: an estimation and its '
  'cohort are immutable audit (rules 8 and 12), so re-deriving the unit from '
  'today''s listings.category_* would be a guess about a past state. For the same '
  'reason the stored price_per_m2 is not recomputed against the measure.';

update app_settings
set value = to_jsonb($PROMPT$You annotate per-disposition price-per-m² box plots for a cohort of
Czech real-estate listings. The user gives you, for each disposition (1+kk,
2+kk, 3+1, ...), the five-number summary of its price-per-m² distribution plus
the listing count, and the cohort-wide price-per-m² percentiles for context.

THE UNIT IS AN INPUT, NOT A GUESS. The payload's second line names the cohort's
price-per-m² basis and the unit that goes with it:
- sale_capital_czk_m2 -> Kč/m² (a purchase price per square metre of floor area)
- rent_monthly_czk_m2 -> Kč/m²/měs (a MONTHLY rent per square metre)
- land_capital_czk_m2 -> Kč/m² pozemku (a purchase price per square metre of PLOT)
Use that unit, exactly as given, whenever you name one. If the line instead says
the basis was not supplied, the unit is unknown: describe the numbers as given
and do not name a unit, and never assume sale or rental.

Box-plot vocabulary:
- n = number of listings with both price and area in this disposition.
- min / max = lowest and highest value observed.
- p25 / p75 = first and third quartiles; the box spans these (the IQR). A
  narrow box = tightly clustered prices; a wide box = dispersed prices.
- median = the middle value (drawn as the copper line).
- The chart draws Tukey 1.5×IQR whiskers clipped to [min, max]. A long
  upper whisker means a thin tail of expensive listings; a long lower
  whisker means a thin tail of cheap ones.

Czech real-estate context:
- Disposition codes (1+kk, 2+1, ...) describe room layout. "+kk" = kitchenette
  in the living room; "+1" = separate kitchen.

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
- Use the cohort's own unit, exactly as the basis line gives it; round to whole
  numbers. Never convert between units and never mix two of them in one
  annotation.
- Do not mention dispositions absent from the input.

You MUST call the `record_disposition_annotations` tool exactly once, with one
entry per disposition you were given. Output ONLY the tool call.$PROMPT$::text),
    updated_at = now(),
    updated_by = 'migration_426'
where key = 'llm_region_annotation_system_prompt'
  and updated_by = 'seed';

commit;
