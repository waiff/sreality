-- 016_locality_ids_extended.sql
-- Promote three more locality IDs from raw_json to typed columns,
-- mirroring migration 004's pattern for region/district.
--
-- locality_municipality_id : sreality's obec (municipality) ID.
-- locality_quarter_id      : sreality's "quarter" — actually the city
--                            ward / městská část (Praha has ~57,
--                            Brno ~29). Sparse outside cities.
-- locality_ward_id         : sreality's "ward" — actually the
--                            katastrální území / cadastral territory
--                            (~13,000 nationally; Praha alone has 112).
--                            sreality's English naming is inverted
--                            relative to the Czech administrative
--                            meaning; we keep the raw_json key names
--                            so the column name maps 1:1 to the
--                            source field.
--
-- Sentinel value -1 in raw_json means "unknown for this listing".
-- We sanitise it to NULL on backfill so the columns mean "the unit
-- this listing belongs to, or NULL when unassigned."
--
-- These IDs are sreality's internal identifiers, NOT ČÚZK / RÚIAN
-- codes. The bridge to ČÚZK polygons is built later via spatial
-- join in migration 017's ingest path.

alter table listings
  add column locality_municipality_id integer,
  add column locality_quarter_id      integer,
  add column locality_ward_id         integer;

create index on listings (locality_municipality_id);
create index on listings (locality_quarter_id);
create index on listings (locality_ward_id);

update listings
set
  locality_municipality_id = nullif(
    (raw_json -> 'recommendations_data' ->> 'locality_municipality_id')::int, -1),
  locality_quarter_id = nullif(
    (raw_json -> 'recommendations_data' ->> 'locality_quarter_id')::int, -1),
  locality_ward_id = nullif(
    (raw_json -> 'recommendations_data' ->> 'locality_ward_id')::int, -1)
where raw_json -> 'recommendations_data' ? 'locality_municipality_id'
   or raw_json -> 'recommendations_data' ? 'locality_quarter_id'
   or raw_json -> 'recommendations_data' ? 'locality_ward_id';
