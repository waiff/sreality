-- 086_scrape_runs.sql
--
-- Per-scrape audit table. Each scrape (full nightly or 15-min delta)
-- writes one row at start and updates it at end with aggregate counts
-- plus a per-category JSONB breakdown. Backs the Health page's
-- "Recent scrapes" section and the image-mirror overview.
--
-- Forward-only: past runs are not recoverable from existing data
-- (the scraper previously emitted run state only to GitHub Actions
-- logs). The table starts empty and fills as new scrapes run.
--
-- Two SECURITY INVOKER RPCs are added in the same migration so anon
-- can read the Health-page data without direct table access:
--   - recent_scrape_runs(p_days int)   — per-run rows for the chart + table
--   - image_storage_overview()         — total vs stored images per category

create table scrape_runs (
  id                   bigserial primary key,
  started_at           timestamptz not null default now(),
  ended_at             timestamptz,
  run_type             text not null check (run_type in ('full', 'delta')),
  index_pages          int  not null default 0,
  listings_found_new   int  not null default 0,
  listings_scraped_new int  not null default 0,
  listings_updated     int  not null default 0,
  listings_inactive    int  not null default 0,
  images_discovered    int  not null default 0,
  images_stored        int  not null default 0,
  errors               int  not null default 0,
  by_category          jsonb not null default '[]'::jsonb
);

create index scrape_runs_started_at_idx on scrape_runs (started_at desc);

alter table scrape_runs enable row level security;
-- No anon policy. Reads go through the SECURITY INVOKER RPCs below,
-- same pattern as estimation_runs / health_summary.

create or replace function recent_scrape_runs(p_days int default 14)
returns setof scrape_runs
language sql
stable
security invoker
as $$
  select *
  from scrape_runs
  where started_at > now() - make_interval(days => p_days)
  order by started_at desc
$$;

grant execute on function recent_scrape_runs(int) to anon;

create or replace function image_storage_overview()
returns jsonb
language sql
stable
security invoker
as $$
  with per_cat as (
    select
      l.category_main,
      l.category_type,
      count(i.id)            as total,
      count(i.storage_path)  as stored
    from listings l
    left join images i on i.sreality_id = l.sreality_id
    group by 1, 2
  )
  select jsonb_build_object(
    'total_images',  (select count(*) from images),
    'stored_images', (select count(*) from images where storage_path is not null),
    'by_category', coalesce((
      select jsonb_agg(
        jsonb_build_object(
          'category_main', category_main,
          'category_type', category_type,
          'total',  total,
          'stored', stored
        )
        order by category_main, category_type
      )
      from per_cat
    ), '[]'::jsonb)
  )
$$;

grant execute on function image_storage_overview() to anon;
