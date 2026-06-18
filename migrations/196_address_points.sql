-- 196: RÚIAN address points — local coordinate->street resolution source.
--
-- Implements docs/design/street-coverage-ruian.md. The residual no-street
-- listings publish no street text, only a precise coordinate; admin_boundaries
-- stops at the municipality polygon and cannot name a street. This table mirrors
-- the ČÚZK RÚIAN "Adresní místa" open dataset (~3M street-bearing points), each
-- with a street + house number + obec code + a building coordinate — the only
-- source that resolves a coordinate to a street offline, for free.
--
-- A mirror of an external dataset (like amenities / admin_boundaries), refreshed
-- wholesale by scripts.ingest_address_points — not history-tracked. Source coords
-- are S-JTSK (EPSG:5514); the ingest transforms them to 4326, so geom here is
-- WGS84 geography, consistent with listings.geom. Only street-bearing points are
-- stored (single-street villages carry no street and can't resolve one).
--
-- The resolver (scripts.backfill_address_point_streets) assigns a street ONLY on
-- an exact, unambiguous match (tight tolerance + a single candidate street + obec
-- cross-check) for precise-coordinate listings — never a town-center geocode.

create table if not exists address_points (
  id            bigint primary key,            -- RÚIAN "Kód ADM"
  street        text not null,                 -- Název ulice
  house_number  text,                          -- Číslo domovní
  obec_id       integer,                       -- Kód obce == admin_boundaries.id (obec), the mig-140 join key
  geom          geography(point, 4326) not null
);

create index if not exists address_points_geom_gix on address_points using gist (geom);
create index if not exists address_points_obec_idx on address_points (obec_id);

comment on table address_points is
  'ČÚZK RÚIAN address points (Adresní místa) — local mirror for coordinate->street '
  'resolution (docs/design/street-coverage-ruian.md). Refreshed wholesale by '
  'scripts.ingest_address_points; geom is WGS84 (transformed from source S-JTSK EPSG:5514).';