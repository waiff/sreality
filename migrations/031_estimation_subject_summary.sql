-- 031_estimation_subject_summary.sql
--
-- Two changes that make the Estimate page navigable:
--
--   1. estimation_runs gets a subject_summary jsonb column. After a
--      successful estimation, the server calls summarize_listing on
--      the subject snapshot and stores the structured summary inline,
--      so the UI can render "location / building / apartment" at the
--      top of the page without an extra round-trip.
--
--   2. The seeded llm_summary_system_prompt is replaced. The new
--      prompt asks the LLM to additionally produce three short
--      paragraphs — location_summary, building_summary,
--      apartment_summary — that surface in the Estimate page's
--      subject block and in the comparables table. The schema
--      extension is enforced in toolkit/summaries.py; cached
--      listing_summaries rows that pre-date this migration are
--      treated as cache-miss on next call and regenerated.
--
-- No new tables. listing_summaries.summary is jsonb — the schema
-- change is enforced at the Python layer, not at the column level.
-- RLS / policies unchanged. The app_settings update fires the
-- existing history trigger from migration 020, so the prior prompt
-- is preserved in app_settings_history.

------------------------------------------------------------------
-- 1. estimation_runs.subject_summary
------------------------------------------------------------------

alter table estimation_runs
  add column subject_summary jsonb;

------------------------------------------------------------------
-- 2. Replace llm_summary_system_prompt with the extended schema
------------------------------------------------------------------

update app_settings
   set value = to_jsonb($PROMPT$You produce a short, structured summary of a single Czech real-estate listing.

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
  the listing attractive. Strict facts only.

- concerns: 0-5 short strings describing factual drawbacks evident from the
  input. If none are evident, return [].

- condition_assessment: one of "excellent" | "good" | "average" | "poor"
  | "unknown".

- target_audience: one of "family" | "couple" | "single_professional"
  | "investor" | "student" | "general".

- location_summary: 1-2 sentences (max 240 chars) describing the listing's
  location. Mention the district / neighbourhood character if stated, transit
  cues, proximity to amenities or landmarks, urban vs. suburban feel. Strict
  facts from the input only.

- building_summary: 1-2 sentences (max 240 chars) describing the building
  itself. Construction material (cihla / panel / skelet), age or era when
  stated, floor count, condition of common areas, lift, energy rating.

- apartment_summary: 1-2 sentences (max 240 chars) describing the apartment
  unit itself. Disposition, usable area, layout cues, condition / fit-out,
  balcony / terrace / parking when present, furnishing state.

Output ONLY the tool call. No prose outside the tool call.$PROMPT$::text)
 where key = 'llm_summary_system_prompt';
