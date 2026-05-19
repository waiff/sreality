-- 082_curated_cities_spatial_relink.sql
--
-- Phase QUAL follow-up — backfill curated_cities.admin_boundary_id via
-- direct spatial containment instead of the obec→okres→kraj name walk
-- migration 081 used.
--
-- Why this exists:
--   The 081 backfill joined obec → okres → kraj on parent_id with
--   lowercased-name match against curated_cities.kraj_name. That
--   matched 0/205 cities the first time admin_boundaries was loaded
--   (current ČÚZK DBF schema renamed the parent columns, so the
--   ingest script's FIELD_CANDIDATES couldn't resolve them and
--   admin_boundaries.parent_id is NULL on every row).
--
--   Spatial containment doesn't need parent_id OR name disambiguation.
--   It asks the cleanest possible question: which obec polygon
--   contains this city's curated centroid? Answer is unique (obce
--   don't overlap) and matches all 205/205 today.
--
-- Predicate:
--   For each curated_cities row with admin_boundary_id IS NULL,
--   find the obec-level admin_boundaries row whose polygon covers
--   the city's centroid (geography). Tiebreak by smallest area, but
--   ties shouldn't occur because obce don't overlap.
--
-- Idempotent: only touches rows where admin_boundary_id IS NULL,
-- so re-runs / replays don't churn existing links. The companion
-- script change updates `relink_curated_cities` in
-- `scripts/ingest_boundaries.py` to use this same predicate, so
-- every future ingest run rebuilds the link without needing this
-- migration to be re-applied.

set local lock_timeout = '5s';

update curated_cities c
   set admin_boundary_id = (
     select b.id
       from admin_boundaries b
      where b.level = 'obec'
        and st_covers(b.geom, c.centroid)
      order by st_area(b.geom::geometry) asc
      limit 1
   )
 where c.admin_boundary_id is null;

do $$
declare
  miss  int;
  total int;
begin
  select count(*) into total from curated_cities;
  select count(*) into miss
    from curated_cities where admin_boundary_id is null;
  raise notice
    'curated_cities polygon-linked: %/%; unmatched: % (radius fallback)',
    total - miss, total, miss;
end $$;
