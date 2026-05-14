-- 051_skill_archive.sql
--
-- Phase AI cleanup: consolidate the rental skill set to one active
-- skill so the operator stops seeing a confusing v1 / full_v1 split.
--
-- Operator decision (recorded in this session): keep
-- rental_estimator_full_v1 as the only active rental skill; archive
-- rental_estimator_v1. The refiner pipeline (slices B + C) already
-- writes new versions through the skills_history trigger from
-- migration 029, so version history is the per-skill audit trail
-- going forward — no need to spawn _vN sibling rows.
--
-- Why a column rather than a delete:
--
--   1. Refiner pipeline (slice C) preserves `skill_refinements.skill_name`
--      pointing at the skill that produced the estimation. Old
--      estimations that ran under v1 still need their skill row to
--      survive so the operator can read why they got the result they
--      did.
--   2. `skill_refinements.skill_name` is a FK to `skills(name)` with
--      `ON DELETE RESTRICT`. We physically can't delete v1 while any
--      refinement row references it.
--   3. Archival is reversible. If the operator changes their mind
--      they UPDATE archived_at = NULL.
--
-- Changes:
--
--   1. `skills.archived_at timestamptz DEFAULT NULL`.
--   2. UPDATE `rental_estimator_v1` SET archived_at = now().
--   3. Same column added to `skills_history` so history rows
--      preserve the archival state at the moment of the snapshot.

begin;

------------------------------------------------------------------
-- 1. Add archived_at to both tables
------------------------------------------------------------------

alter table skills
  add column if not exists archived_at timestamptz default null;

alter table skills_history
  add column if not exists archived_at timestamptz default null;

comment on column skills.archived_at is
  'Soft-archive timestamp. Non-null skills are hidden from the '
  'default skill list / new-estimation flow but still load by name '
  'so past estimations referencing them stay readable.';

------------------------------------------------------------------
-- 2. Archive rental_estimator_v1
------------------------------------------------------------------

update skills
   set archived_at = now(),
       updated_by  = 'seed'
 where name = 'rental_estimator_v1'
   and archived_at is null;

commit;
