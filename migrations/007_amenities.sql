-- 007_amenities.sql
-- Cache for external Points-of-Interest data, sourced from OpenStreetMap
-- via Overpass API in v1. Future sources (mapy.cz, manual curation)
-- distinguished by the `source` column.
--
-- Two tables, separated because they answer different questions:
--   amenities         - the POI rows themselves (one row per real-world
--                       feature). Spatial-indexed for radius queries.
--   amenity_fetches   - audit + cache-key table. Records each Overpass
--                       call we made. A query checks here first; if a
--                       row covering this (center, radius, category,
--                       freshness) exists, we serve from `amenities`
--                       without hitting Overpass.
--
-- Why two tables instead of stamping `fetched_at` on each amenity:
--   Two distinct queries can return overlapping POI sets. Stamping
--   per-row collapses the question "have we already fetched THIS area?"
--   with the question "when did we last see this individual POI?". The
--   audit table answers the first cheaply via spatial index over
--   centers; the amenity table answers the second.
--
-- Cache invalidation: a fetch row older than the caller's TTL (default
-- 30 days, see toolkit/amenities.py) is treated as a miss. Stale rows
-- are kept for the audit trail; manual SQL pruning when the table
-- grows.
--
-- Free-text `category` (not enum): we expect to add categories as use
-- cases emerge (see CLAUDE.md taxonomy in toolkit/amenities.py). An
-- enum would force a migration per addition. Consolidate after we see
-- what real values arrive.

create table amenities (
  id           bigserial primary key,
  source       text        not null,
  source_id    text        not null,
  category     text        not null,
  name         text,
  geom         geography(point, 4326) not null,
  raw_json     jsonb,
  fetched_at   timestamptz not null default now(),
  unique (source, source_id)
);

create index on amenities using gist (geom);
create index on amenities (category);
create index on amenities (source, fetched_at desc);

create table amenity_fetches (
  id             bigserial primary key,
  center_geom    geography(point, 4326) not null,
  radius_m       integer     not null,
  category       text        not null,
  source         text        not null default 'osm',
  fetched_at     timestamptz not null default now(),
  amenity_count  integer     not null
);

create index on amenity_fetches using gist (center_geom);
create index on amenity_fetches (category, fetched_at desc);

alter table amenities       enable row level security;
alter table amenity_fetches enable row level security;
