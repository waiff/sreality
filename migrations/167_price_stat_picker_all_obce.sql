-- 167_price_stat_picker_all_obce.sql
--
-- Open the price-stats municipality picker to EVERY obec, matching the MF
-- "Cenová mapa nájemného" obec breakdown (full Czech territory).
--
-- Previously the picker (and resolve_obce) were limited to obce with a
-- precomputed admin_boundaries.sreality_id, which was populated by a spatial
-- join from listing POINTS (scripts/ingest_boundaries.py) — so ~2,535 obce
-- with no listing points fell out, even though sreality's locality DB has an
-- entity for essentially every Czech municipality. The scraper now resolves
-- a dataset's unmapped obce on demand via localities/suggest + coordinate PIP
-- (scraper.price_stats_main), so the picker no longer needs the sreality_id
-- gate. Obce without data still surface as "insufficient" (migration 164).

create or replace view price_stat_obce_picker_public as
  select id, level, name, parent_id, population, sreality_id
    from admin_boundaries
   where level in ('kraj', 'okres', 'obec');
