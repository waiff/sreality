-- 004_locality_ids.sql
-- Promote two stable integer IDs from raw_json to typed columns.
-- locality_district_id and locality_region_id come from the
-- recommendations_data block of every detail response. They are
-- far more reliable than the current free-text `district` column,
-- which is parsed from the locality string and varies in format.
--
-- The existing `district` column is kept untouched for backward
-- compatibility and human-readable display. New code should prefer
-- the IDs for filtering and grouping.

alter table listings
  add column locality_district_id integer,
  add column locality_region_id   integer;

create index on listings (locality_district_id);
create index on listings (locality_region_id);

update listings
set
  locality_district_id = (raw_json -> 'recommendations_data' ->> 'locality_district_id')::integer,
  locality_region_id   = (raw_json -> 'recommendations_data' ->> 'locality_region_id')::integer
where raw_json -> 'recommendations_data' ? 'locality_district_id'
   or raw_json -> 'recommendations_data' ? 'locality_region_id';
