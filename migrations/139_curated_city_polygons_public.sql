-- 139_curated_city_polygons_public.sql
--
-- City-quality map overlay: expose each curated city's municipality
-- boundary (the RÚIAN obec polygon ingested by migration 017 and linked
-- to curated_cities.admin_boundary_id in migration 081) to the browser
-- as simplified GeoJSON, so the Browse map can draw the real municipality
-- shape instead of a fixed-radius circle.
--
-- Anon-read view, the standard *_public privilege-boundary pattern: anon
-- gets SELECT on the view, the view owner reads the (RLS-on) base tables.
-- The geometry is simplified (ST_SimplifyPreserveTopology, ~55 m at this
-- 0.0005° tolerance) and emitted as a GeoJSON string the frontend
-- JSON.parses into a Feature geometry — the SAME contract as
-- rent_map_choropleth_public.
--
-- Live (NOT materialized) on purpose: the join is a 205-row PK lookup +
-- simplify that EXPLAIN ANALYZE clocks at ~90 ms, comfortably inside
-- anon's 3 s statement_timeout, and the boundaries are static reference
-- data the frontend fetches once per session and caches forever.

create or replace view curated_city_polygons_public as
  select
    c.id as city_id,
    st_asgeojson(
      st_simplifypreservetopology(b.geom::geometry, 0.0005), 5
    ) as geojson
  from curated_cities c
  join admin_boundaries b on b.id = c.admin_boundary_id
  where c.admin_boundary_id is not null;

grant select on curated_city_polygons_public to anon;
