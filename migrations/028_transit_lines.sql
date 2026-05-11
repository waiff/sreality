-- 028_transit_lines.sql
-- OSM transit-line cache, parallel to the amenities mirror in 007.
--
-- amenities (007) caches *point* POIs. find_comparables_along_axis
-- (toolkit/transit_axis.py) needs *line* geometry — the route
-- relations behind a tram, metro, or bus line — to compute
-- "comparables in a corridor along this transit line". That is a
-- materially different shape than a node, so it gets its own pair
-- of tables rather than overloading `amenities`.
--
-- Two tables, same split rationale as 007:
--   transit_lines         - one row per OSM route relation (tram 9,
--                           metro A, bus 112). Spatial-indexed for
--                           ST_DWithin against an anchor point and
--                           for the corridor ST_DWithin against
--                           listings.
--   transit_line_fetches  - audit + cache-key table. Records each
--                           Overpass call we made for a bbox+types
--                           combo. A query checks here first; if a
--                           recent row covers this (bbox, types,
--                           freshness) we serve from `transit_lines`
--                           without re-hitting Overpass.
--
-- Cache invalidation: a fetch row older than the caller's TTL
-- (default 30 days, matching the amenity TTL) is treated as a miss.
-- Stale rows are kept for the audit trail; manual SQL pruning when
-- the table grows. Same discipline as `amenity_fetches`.
--
-- Why bbox cache keys (not center-circle): route relations span
-- many kilometres; the natural query shape is "lines passing through
-- this bounding box", not "lines whose center is within X of this
-- point". `query_hash` is the sha256 of canonicalised
-- (bbox, transport_types) so callers using identical params share
-- the same cache row.
--
-- transport_type enum kept narrow on purpose: tram / subway / bus
-- are the three we currently care about for tenant proximity in CZ.
-- Trolleybus or train route relations exist in OSM but are out of
-- scope until we have a reason. Extending is a single CHECK update.

create table transit_lines (
  id             bigserial primary key,
  source         text        not null default 'osm',
  source_id      text        not null,
  transport_type text        not null
    check (transport_type in ('tram', 'subway', 'bus')),
  route_ref      text,
  name           text,
  geom           geography(linestring, 4326) not null,
  raw_json       jsonb,
  fetched_at     timestamptz not null default now(),
  unique (source, source_id)
);

create index on transit_lines using gist (geom);
create index on transit_lines (transport_type);
create index on transit_lines (source, fetched_at desc);

create table transit_line_fetches (
  id              bigserial primary key,
  query_hash      text        not null,
  bbox_minlat     double precision not null,
  bbox_minlng     double precision not null,
  bbox_maxlat     double precision not null,
  bbox_maxlng     double precision not null,
  transport_types text[]      not null,
  source          text        not null default 'osm',
  fetched_at      timestamptz not null default now(),
  line_count      integer     not null
);

create index on transit_line_fetches (query_hash, fetched_at desc);
create index on transit_line_fetches (source, fetched_at desc);

alter table transit_lines        enable row level security;
alter table transit_line_fetches enable row level security;
