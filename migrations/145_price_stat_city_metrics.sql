-- 145_price_stat_city_metrics.sql
--
-- Precomputed per-(dataset, locality) derived metrics for the analysis tab +
-- map heat layers, recomputed at the end of each ingestion run by
-- scraper/price_stats_db.recompute_metrics. Precomputed (not live) because the
-- Browse/Datasets map reads run as anon under a 3 s statement timeout — same
-- reasoning as rent_map_choropleth (migration 132).
--
--   sale_*  — prodej series rollups (price = Kč/m²)
--   rent_*  — pronájem series rollups (price = Kč/m²/month)
--   *_cagr_pct      — compound annual growth over `window_years` (NULL if the
--                     series is too short / thin to be meaningful)
--   *_min_active    — min active_count over the window (sparsity flag; the UI
--                     greys out thin cohorts — the real cause of the "values
--                     jumping 50%" the old DOM scraper hit)
--   gross_yield_pct — 12 × rent_per_m² / sale_per_m² × 100 (per-m² cancels)

set local lock_timeout = '5s';

create table price_stat_city_metrics (
  dataset_id         bigint not null
                       references price_stat_datasets(id) on delete cascade,
  entity_type        text not null,
  entity_id          integer not null,
  obec_id            bigint,
  window_years       integer not null,
  sale_latest_price  integer,
  sale_latest_ym     text,
  sale_cagr_pct      double precision,
  sale_months        integer,
  sale_min_active    integer,
  rent_latest_price  integer,
  rent_latest_ym     text,
  rent_cagr_pct      double precision,
  rent_months        integer,
  rent_min_active    integer,
  gross_yield_pct    double precision,
  computed_at        timestamptz not null default now(),
  primary key (dataset_id, entity_type, entity_id),
  foreign key (entity_type, entity_id)
    references price_stat_localities (entity_type, entity_id)
);

create index price_stat_city_metrics_obec_idx
  on price_stat_city_metrics (dataset_id, obec_id);


-- Choropleth pivot for the map: per-(dataset, obec) metrics joined to the
-- simplified obec polygon, GeoJSON at 5 decimals (~1 m). Materialized + keyed
-- by (dataset_id, obec_id) so the anon read is a precomputed scan; REFRESHed
-- CONCURRENTLY at the end of each run.
create materialized view price_stat_choropleth as
  select m.dataset_id,
         b.id                              as obec_id,
         b.name                            as obec_name,
         st_asgeojson(b.geom::geometry, 5) as geojson,
         m.sale_cagr_pct,
         m.rent_cagr_pct,
         m.gross_yield_pct,
         m.sale_latest_price,
         m.rent_latest_price,
         m.sale_min_active,
         m.rent_min_active
    from price_stat_city_metrics m
    join admin_boundaries b on b.id = m.obec_id
   where m.obec_id is not null;

create unique index price_stat_choropleth_pk
  on price_stat_choropleth (dataset_id, obec_id);


-- --- public read views (anon) ----------------------------------------------

create view price_stat_city_metrics_public as
  select m.dataset_id, m.entity_type, m.entity_id, l.name as locality_name,
         m.obec_id, m.window_years,
         m.sale_latest_price, m.sale_latest_ym, m.sale_cagr_pct,
         m.sale_months, m.sale_min_active,
         m.rent_latest_price, m.rent_latest_ym, m.rent_cagr_pct,
         m.rent_months, m.rent_min_active,
         m.gross_yield_pct, m.computed_at
    from price_stat_city_metrics m
    join price_stat_localities l
      on l.entity_type = m.entity_type and l.entity_id = m.entity_id;

create view price_stat_choropleth_public as
  select dataset_id, obec_id, obec_name, geojson, sale_cagr_pct, rent_cagr_pct,
         gross_yield_pct, sale_latest_price, rent_latest_price,
         sale_min_active, rent_min_active
    from price_stat_choropleth;


alter table price_stat_city_metrics enable row level security;

grant select on price_stat_city_metrics_public to anon;
grant select on price_stat_choropleth_public   to anon;
