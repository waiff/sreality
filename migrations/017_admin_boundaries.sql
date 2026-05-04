-- 017_admin_boundaries.sql
--
-- Polygon geometries for Czech administrative units (kraj / okres /
-- obec / katastrální území). Populated by the `ingest_boundaries`
-- workflow from ČÚZK RÚIAN public data; this file only creates the
-- schema. Counts after a successful ingest:
--   kraj  ~14, okres ~76, obec ~6,250, ku ~13,000.
--
-- Identity: the row's `id` is the official ČÚZK / RÚIAN code for the
-- unit. ČÚZK is the canonical source of Czech boundary data, so we
-- key by their ID and never invent our own. RÚIAN codes are stable
-- across years.
--
-- Bridge to sreality: a separate `sreality_id` column stores the
-- internal sreality identifier for the same unit, populated by
-- point-in-polygon spatial join against `listings.geom` during
-- ingest. Sreality uses its own ID space — see Part A of map-1's
-- inspection report. Aggregation joins `listings.locality_*_id` to
-- `admin_boundaries.sreality_id`, NOT to `id`. A unit with no
-- listing points inside it gets sreality_id = NULL, which is fine
-- (we can't aggregate over a polygon we have no data in).
--
-- Hierarchy walks via `parent_id`, not denormalised columns:
--   ku.parent_id = obec.id
--   obec.parent_id = okres.id
--   okres.parent_id = kraj.id
--   kraj.parent_id = NULL
--
-- Geometry: MULTIPOLYGON because some obec have exclaves. Stored as
-- geography(MULTIPOLYGON, 4326) so spatial joins use the same SRID
-- as `listings.geom`. Polygons are pre-simplified per level by the
-- ingest script (100 m kraj / 75 m okres / 50 m obec / 20 m ku) so
-- they're cheap to render in the choropleth UI.

create table admin_boundaries (
  id           bigint primary key,
  level        text not null check (level in ('kraj', 'okres', 'obec', 'ku')),
  name         text not null,
  parent_id    bigint references admin_boundaries(id) on delete set null,
  sreality_id  integer,
  geom         geography(multipolygon, 4326) not null,
  area_km2     numeric(10, 3),
  ingested_at  timestamptz not null default now()
);

create index on admin_boundaries using gist (geom);
create index on admin_boundaries (level);
create index on admin_boundaries (parent_id);
create index on admin_boundaries (level, parent_id);
create index on admin_boundaries (level, sreality_id);

alter table admin_boundaries enable row level security;

-- Public read for the choropleth UI (map-2). Same anon-grant pattern
-- as migration 008's *_public views.
create view admin_boundaries_public as
select id, level, name, parent_id, sreality_id, geom, area_km2
from admin_boundaries;

grant select on admin_boundaries_public to anon;
