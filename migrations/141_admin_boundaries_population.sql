-- 141_admin_boundaries_population.sql
--
-- Population for EVERY municipality (obec), not just the 206 curated cities.
--
-- city_population (migration 078) only covers curated_cities. The official ČSÚ
-- DataStat export "Počet obyvatel v obcích k 1. 1." (OBY02AT02, committed at
-- data/csu_population.json) carries all ~6 000 obce, keyed by the municipality
-- code that IS admin_boundaries.id (the RÚIAN obec code — verified: every obec
-- id is in the ČSÚ code range). Storing it on admin_boundaries makes "within
-- X km of a municipality with population > N" answerable across the whole
-- country, and lets a listing's home-municipality population drive the Min
-- Population filter (migration 142).
--
-- Loaded by scripts/load_obec_population.py (reads the committed JSON, upserts
-- by id). NULL where the export has no figure for that obec; not an error.

alter table admin_boundaries
  add column if not exists population      integer,
  add column if not exists population_year integer;

comment on column admin_boundaries.population is
  'Population (ČSÚ "Počet obyvatel k 1.1.", OBY02AT02). Obec level only; '
  'NULL elsewhere / unknown. Loaded by scripts/load_obec_population.py.';

create index if not exists admin_boundaries_population_idx
  on admin_boundaries (population)
  where level = 'obec' and population is not null;
