-- 132_rent_map.sql
--
-- Source store for the MF "Cenová mapa nájemného" (rent price map), ingested
-- with full revision history. Modelled on the curated-cities store
-- (migration 078): append-only *_revisions audit, long-form values, and
-- latest-revision-wins *_public views.
--
-- Storage model:
--   rent_map_revisions   — one row per ingested XLSX (auto-grab or manual
--                          upload). file_sha256 UNIQUE ⇒ re-ingesting an
--                          unchanged file is a no-op.
--   rent_map_values      — long-form (revision, RÚIAN code, VK) reference
--                          rent per m², standard + novostavba. The territory
--                          key `ruian_code` IS admin_boundaries.id; `level`
--                          ('ku'|'obec') disambiguates which boundary level.
--   rent_map_adjustments — per-(VK, novostavba) amenity adjustment Kč/m².
--   rent_map_choropleth  — materialized pivot (VK1..VK4 rents joined to
--                          admin_boundaries geometry) for the Browse map; a
--                          precomputed scan so the anon read stays under the
--                          3 s statement timeout. REFRESHed after each ingest.
--
-- All base tables RLS-on; anon reads only through the _public views.

set local lock_timeout = '5s';

create table rent_map_revisions (
  source_revision  bigserial primary key,
  source_date      date,
  source_filename  text not null,
  file_sha256      text not null unique,
  row_count        integer not null,
  uploaded_by      text,
  uploaded_at      timestamptz not null default now()
);


create table rent_map_values (
  source_revision             bigint not null
    references rent_map_revisions(source_revision) on delete cascade,
  ruian_code                  bigint not null,
  level                       text not null check (level in ('ku', 'obec')),
  kraj                        text,
  ku_name                     text,
  obec_name                   text,
  vk                          smallint not null check (vk between 1 and 4),
  ref_rent_per_m2             integer,
  ref_rent_novostavba_per_m2  integer,
  data_coverage               smallint,
  primary key (source_revision, ruian_code, vk)
);

create index rent_map_values_code_idx on rent_map_values (ruian_code, vk);


create table rent_map_adjustments (
  source_revision  bigint not null
    references rent_map_revisions(source_revision) on delete cascade,
  vk               smallint not null check (vk between 1 and 4),
  is_novostavba    boolean not null,
  attribute        text not null,
  czk_per_m2       integer not null,
  primary key (source_revision, vk, is_novostavba, attribute)
);


-- --- public read views (anon): latest revision wins -------------------------

create view rent_map_values_public as
  select ruian_code, level, kraj, ku_name, obec_name, vk,
         ref_rent_per_m2, ref_rent_novostavba_per_m2, data_coverage,
         source_revision
    from rent_map_values
   where source_revision = (select max(source_revision) from rent_map_revisions);


create view rent_map_adjustments_public as
  select vk, is_novostavba, attribute, czk_per_m2, source_revision
    from rent_map_adjustments
   where source_revision = (select max(source_revision) from rent_map_revisions);


-- Choropleth pivot for the Browse map. Materialized: ~7.6k simplified
-- polygons joined to their reference rents, computed once per ingest rather
-- than on every anon page load. Geometry emitted as GeoJSON at 5 decimals
-- (~1 m) to keep the payload small.
create materialized view rent_map_choropleth as
  select
    b.id                                    as ruian_code,
    b.level,
    b.name,
    min(v.kraj)                             as kraj,
    st_asgeojson(b.geom::geometry, 5)       as geojson,
    max(v.ref_rent_per_m2) filter (where v.vk = 1) as vk1_per_m2,
    max(v.ref_rent_per_m2) filter (where v.vk = 2) as vk2_per_m2,
    max(v.ref_rent_per_m2) filter (where v.vk = 3) as vk3_per_m2,
    max(v.ref_rent_per_m2) filter (where v.vk = 4) as vk4_per_m2
  from rent_map_values v
  join admin_boundaries b
    on b.id = v.ruian_code and b.level = v.level
  where v.source_revision = (select max(source_revision) from rent_map_revisions)
  group by b.id;

create unique index rent_map_choropleth_pk on rent_map_choropleth (ruian_code);

create view rent_map_choropleth_public as
  select ruian_code, level, name, kraj, geojson,
         vk1_per_m2, vk2_per_m2, vk3_per_m2, vk4_per_m2
    from rent_map_choropleth;


-- Kraj boundaries for the map's optional "Kraje" overlay (14 rows).
create view rent_map_kraje_public as
  select id as ruian_code, name, st_asgeojson(geom::geometry, 4) as geojson
    from admin_boundaries
   where level = 'kraj';


alter table rent_map_revisions   enable row level security;
alter table rent_map_values      enable row level security;
alter table rent_map_adjustments enable row level security;

grant select on rent_map_values_public      to anon;
grant select on rent_map_adjustments_public to anon;
grant select on rent_map_choropleth_public  to anon;
grant select on rent_map_kraje_public       to anon;
