-- 140_relink_curated_cities_obec_by_name.sql
--
-- Correct the handful of curated_cities that migration 081's name-walk
-- linked to the WRONG obec — a larger, differently-named neighbour:
--   Šlapanice → Brno,  Odry → Ostrava,  Hranice / Jeseník → Olomouc,
--   Chrudim → České Lhotice,  Mělník → Úžice.
-- The new city-overlay polygons (migration 139) made the mislink obvious
-- as a giant blob far from the city, and it ALSO mis-scoped those cities'
-- city-quality filter — `listings_with_city_quality` / `browse_stats` use
-- ST_Covers(boundary, listing) against admin_boundary_id, so e.g. the
-- "Šlapanice" filter was matching all of Brno's listings.
--
-- Re-link by exact obec NAME match, tie-broken by the nearest centroid.
-- Pure spatial containment is NOT safe here: several curated centroids
-- (Mapy.cz geocodes) land just outside the town in a tiny neighbour, so
-- ST_Covers would BREAK correct links (Třeboň→Hartmanice, Svitavy→České
-- Heřmanice, Konice→Brodek u Konice). A name match only changes a row when
-- a confident same-name obec exists AND differs from the current link, so
-- the already-correct links and the cities with no same-name obec are left
-- untouched. Idempotent: re-running is a no-op once linked.

update curated_cities c
   set admin_boundary_id = m.obec_id
  from (
    select c2.id as city_id,
           (
             select b.id
               from admin_boundaries b
              where b.level = 'obec'
                and lower(b.name) = lower(c2.name)
              order by b.geom <-> c2.centroid asc
              limit 1
           ) as obec_id
      from curated_cities c2
  ) m
 where m.city_id = c.id
   and m.obec_id is not null
   and m.obec_id is distinct from c.admin_boundary_id;
