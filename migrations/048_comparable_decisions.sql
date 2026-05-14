-- 048_comparable_decisions.sql
--
-- (Originally drafted as 046; bumped to 048 alongside the
-- 047 → 049_estimation_feedback and 048 → 050_skill_refinements
-- renames to clear slots 046 / 047 claimed by main's
-- manual_rental_estimates work. The live DB has the original
-- migration name recorded in supabase_migrations.schema_migrations
-- already — this rename is filename-only.)
--
-- Audit improvement (Phase AI follow-up to slice A): the agent now
-- emits a per-listing decision log alongside `comparables_used` so
-- the operator can see WHY each candidate was kept or set aside.
-- Without this, /estimation/N gives a confidence score and a list
-- of IDs but no per-row reasoning — which is exactly what slice A's
-- whole reason for being is.
--
-- Changes in this migration:
--
--   1. estimation_runs.comparables_excluded jsonb — parallel column
--      to comparables_used storing the {sreality_id, reason} of every
--      candidate the agent considered and chose not to include.
--      Null on deterministic runs and on legacy agent rows predating
--      this migration.
--
--   2. skills.system_prompt — both rental skills get a new bullet on
--      the `record_estimate` arguments list requiring
--      `comparable_decisions` so the agent emits the per-listing
--      decision log. Done as a string-replace anchored on the
--      existing `comparables_used` line, so prior operator edits to
--      other rules survive. The trigger from migration 029 writes a
--      `skills_history` row automatically.
--
-- Storage shape on estimation_runs.comparables_used:
--
--   Each entry now optionally carries a `reason` string in addition
--   to the existing {sreality_id, snapshot_id, snapshot_date,
--   data_age_days, verified_during_estimate} fields. JSONB tolerates
--   the extra key without a schema change, so legacy readers ignore
--   it. The frontend's ComparableUsed type adds `reason?: string`.
--
-- Storage shape on estimation_runs.comparables_excluded:
--
--   list[{sreality_id: int, reason: str}]   — null on deterministic
--   runs and on legacy agent rows.

begin;

------------------------------------------------------------------
-- 1. Add comparables_excluded column
------------------------------------------------------------------

alter table estimation_runs
  add column if not exists comparables_excluded jsonb default null;

comment on column estimation_runs.comparables_excluded is
  'Per-listing reasons for candidates the agent considered and did '
  'not include in comparables_used. Shape: '
  '[{sreality_id: int, reason: str}]. Null on deterministic runs '
  'and on agent runs predating migration 048.';

------------------------------------------------------------------
-- 2. Skill prompt updates — anchor on the existing
--    `comparables_used` bullet and insert a new
--    `comparable_decisions` bullet immediately before it.
------------------------------------------------------------------

-- rental_estimator_v1 (3-space indented sub-bullets)
update skills
   set system_prompt = replace(
         system_prompt,
         '   - comparables_used: list of sreality_id from the cohort you actually',
         '   - comparable_decisions: REQUIRED. One entry per candidate '
       || 'you considered (every sreality_id in the cohort you '
       || 'analysed). Each entry has sreality_id (int), '
       || 'decision (''included'' or ''excluded''), and a '
       || 'one-sentence reason. Every entry with decision=''included'' '
       || 'must also appear in comparables_used. Entries with '
       || 'decision=''excluded'' name listings you saw and set aside — '
       || 'this is the audit trail the operator reads to understand '
       || 'why a particular comp did or did not shape the range.'
       || E'\n   - comparables_used: list of sreality_id from the cohort you actually'
       ),
       updated_by = 'seed'
 where name = 'rental_estimator_v1';

-- rental_estimator_full_v1 (4-space indented sub-bullets)
update skills
   set system_prompt = replace(
         system_prompt,
         '    - comparables_used: list of sreality_id from the cohort you actually',
         '    - comparable_decisions: REQUIRED. One entry per candidate '
       || 'you considered (every sreality_id in the cohort you '
       || 'analysed across all rounds, including ones merged in via '
       || '`find_comparables_along_axis`). Each entry has '
       || 'sreality_id (int), decision (''included'' or ''excluded''), '
       || 'and a one-sentence reason. Every entry with '
       || 'decision=''included'' must also appear in comparables_used. '
       || 'Entries with decision=''excluded'' name listings you saw '
       || 'and set aside — this is the audit trail the operator reads '
       || 'to understand why a particular comp did or did not shape '
       || 'the range.'
       || E'\n    - comparables_used: list of sreality_id from the cohort you actually'
       ),
       updated_by = 'seed'
 where name = 'rental_estimator_full_v1';

commit;
