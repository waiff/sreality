-- 047_skill_manual_estimates_tool.sql
--
-- (Originally drafted as 046; bumped alongside the 043 → 046 rename of
-- manual_rental_estimates.sql. Both files were renumbered before merge;
-- the live DB has both applied — this rename is filename-only.)
--
-- Wires the `get_manual_rental_estimates` tool (added by migration 046 +
-- new toolkit/manual_estimates.py) into the estimator skills.
--
-- Same shape as migration 045 (`read_floor_plan` add): only the
-- `allowed_tools` array is touched here. The system_prompt update
-- that wires the new "CONSULT MANUAL ESTIMATES" step into the
-- agent's instructions is intentionally left to a separate operator
-- action via the Settings UI — `skills.system_prompt` may have
-- been hand-edited by the operator (the `skills_history` trigger
-- preserves prior versions) and we must not overwrite those edits.
-- The on-disk `skills/<name>/SKILL.md` files in the same commit
-- carry the canonical numbered-step text for reference.

update skills
   set allowed_tools = allowed_tools || '["get_manual_rental_estimates"]'::jsonb,
       updated_by    = 'seed'
 where name in ('rental_estimator_v1', 'rental_estimator_full_v1')
   and not (allowed_tools @> '["get_manual_rental_estimates"]'::jsonb);
