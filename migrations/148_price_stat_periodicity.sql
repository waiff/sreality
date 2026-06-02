-- 148_price_stat_periodicity.sql
--
-- Per-dataset sampling periodicity for the price-stats ingestion. The
-- estate_prices API always returns a MONTHLY series; `periodicity` controls
-- which of those months we keep (the period-end / last-available month per
-- bucket) so a dataset can be stored monthly / quarterly / semiannual /
-- annual within its scrape window. The growth RPC annualizes from the actual
-- (year, month) indices, so CAGR + yield-change stay correct at any spacing.

set local lock_timeout = '5s';

alter table price_stat_datasets
  add column periodicity text not null default 'monthly'
    check (periodicity in ('monthly', 'quarterly', 'semiannual', 'annual'));

create or replace view price_stat_datasets_public as
  select id, slug, name, description, category_main_cb, building_condition,
         building_type, ownership, usable_area_from, usable_area_to, distance,
         is_active, created_at, updated_at,
         start_ym, end_ym, obec_ids, min_population, max_population, periodicity
    from price_stat_datasets
   where is_active;
