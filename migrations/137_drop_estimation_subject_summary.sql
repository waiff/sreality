-- 137_drop_estimation_subject_summary.sql
--
-- Remove the estimation "subject summary" feature. The LLM-generated summary
-- of an estimation's subject listing (added in migration 031) duplicated the
-- Listing-Detail summary and added noise + cost to every run; the estimation
-- page no longer renders it.
--
-- This drops ONLY estimation_runs.subject_summary. It does NOT touch:
--   * toolkit.summaries.summarize_listing / listing_summaries (still used by
--     Listing Detail + the comparables modal),
--   * app_settings.llm_summary_system_prompt / llm_summary_model (shared with
--     that feature — migration 031's prompt extension stays),
--   * building_runs.subject_summary (migration 035 — a different, structural
--     display payload for the building-paste flow).
--
-- Destructive: the 23 non-null rows live on were copied to
-- _backup_estimation_subject_summary_20260602 before this ran (drop that
-- backup table manually once you're confident).

ALTER TABLE estimation_runs DROP COLUMN IF EXISTS subject_summary;
