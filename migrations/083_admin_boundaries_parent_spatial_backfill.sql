-- 083_admin_boundaries_parent_spatial_backfill.sql
--
-- Phase QUAL follow-up — populate admin_boundaries.parent_id via
-- direct PostGIS containment so the four-level hierarchy
-- (kraj ← okres ← obec ← ku) is actually walkable.
--
-- Why this exists:
--   Migration 017 added the parent_id self-FK; the ingest script in
--   scripts/ingest_boundaries.py was meant to fill it from per-level
--   DBF columns (KOD_KR_, KOD_OK_, KOD_OB_). The current ČÚZK
--   shapefile pack renamed those columns, so FIELD_CANDIDATES in the
--   script no longer recognises them and parent_id ended up NULL on
--   every row after the PR #157/#158 load. Migration 082 worked
--   around the gap for curated_cities specifically by going straight
--   to spatial containment. This migration applies the same
--   technique to the hierarchy itself.
--
-- admin_boundaries.id IS the RÚIAN code itself (migration 017 lines
-- 9-12), not a synthetic sequence, so parent_id is just the parent's
-- RÚIAN code stored in the child row. The UPDATE below writes that
-- integer directly.
--
-- Predicate:
--   For each non-kraj row where parent_id IS NULL, find the row at
--   the next-higher level whose polygon covers a representative point
--   of the child polygon. ST_PointOnSurface (not ST_Centroid) because
--   some obce are concave or annular and their centroid can fall
--   outside the polygon entirely. ST_Covers (not ST_Contains)
--   because the geometries are simplified (50 m for obec, 75 m for
--   okres) and we want boundary-touch to count.
--
-- Edge case:
--   If a child's representative point doesn't land in any parent
--   polygon (border simplification artefact), leave parent_id NULL.
--   Do not fall back to nearest — NULL is honest; nearest would
--   silently mis-parent a row.
--
-- Idempotent: the WHERE clause `parent_id IS NULL` means replays
-- never churn rows the DBF path (now or in the future) managed to
-- populate. The companion script change in scripts/ingest_boundaries.py
-- adds a `populate_parent_ids_spatial` step to run_pipeline so every
-- future ingest re-establishes the hierarchy without needing this
-- migration to be re-applied.

set local lock_timeout = '5s';

update admin_boundaries c
   set parent_id = (
     select p.id
       from admin_boundaries p
      where p.level = case c.level
                        when 'okres' then 'kraj'
                        when 'obec'  then 'okres'
                        when 'ku'    then 'obec'
                      end
        and st_covers(
              p.geom,
              st_pointonsurface(c.geom::geometry)::geography)
      order by st_area(p.geom::geometry) asc
      limit 1
   )
 where c.level <> 'kraj'
   and c.parent_id is null;

do $$
declare
  rec record;
begin
  for rec in
    select level,
           count(*)                       as total,
           count(parent_id)               as with_parent,
           count(*) - count(parent_id)    as null_parent
      from admin_boundaries
     where level <> 'kraj'
     group by level
     order by case level when 'okres' then 1
                        when 'obec'  then 2
                        when 'ku'    then 3 end
  loop
    raise notice
      'admin_boundaries.parent_id level=% with_parent=%/% null=%',
      rec.level, rec.with_parent, rec.total, rec.null_parent;
  end loop;
end $$;
