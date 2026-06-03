-- 150_price_stat_run_progress.sql
--
-- Live scrape-progress for the Datasets page. The ingestion updates the run
-- row incrementally (cities_done / cities_total + observations so far); the
-- frontend polls the latest run per dataset while it's 'running' and shows a
-- progress banner. price_stat_runs_public exposes the latest run per dataset
-- to anon (the base table stays RLS-on, read only through the view).

set local lock_timeout = '5s';

alter table price_stat_runs
  add column cities_total integer not null default 0,
  add column cities_done  integer not null default 0;

create view price_stat_runs_public as
  select distinct on (dataset_id)
         dataset_id, id as run_id, status, cities_total, cities_done,
         observations, error, started_at, finished_at
    from price_stat_runs
   order by dataset_id, id desc;

grant select on price_stat_runs_public to anon;
