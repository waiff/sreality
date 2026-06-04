-- 164_price_stat_no_data.sql
--
-- Track price-stats municipalities the scraper checked and found INSUFFICIENT
-- data for (no prodej AND no pronájem series for the dataset's filter). Today a
-- dataless obec leaves no record, so localities_ordered() sorts it as
-- "never scraped" (NULL fetched_at, NULLS FIRST) and every run re-checks the
-- whole dataless tail first — wasting the time budget and making the progress
-- bar look like it restarts from scratch. With a marker the scraper can skip it
-- (TTL re-check) and the UI can show "checked, insufficient data".
--
-- One marker per (dataset, locality); the scraper upserts checked_at when both
-- categories come back empty and DELETEs it the moment a locality yields data.

create table if not exists price_stat_locality_no_data (
  dataset_id  bigint      not null,
  entity_type text        not null,
  entity_id   integer     not null,
  checked_at  timestamptz not null default now(),
  primary key (dataset_id, entity_type, entity_id)
);

create index if not exists price_stat_no_data_dataset_idx
  on price_stat_locality_no_data (dataset_id);

comment on table price_stat_locality_no_data is
  'Per-(dataset, locality) marker: the scraper checked this obec and found no '
  'prodej/pronájem series. Drives the localities_ordered TTL skip + the UI '
  '"insufficient data" surfaces. Deleted when the locality later yields data.';

-- Lightweight anon-readable list (id + name) for the Datasets table greyed rows,
-- the Datasets infopanel count, and the Browse market-growth note.
create or replace view price_stat_no_data_public as
  select nd.dataset_id, l.obec_id, l.name as locality_name
    from price_stat_locality_no_data nd
    join price_stat_localities l using (entity_type, entity_id)
   where l.obec_id is not null;

grant select on price_stat_no_data_public to anon;
