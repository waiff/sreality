-- 087_scrape_runs_public_anon_reads.sql
--
-- Fixes anon visibility for the two Health-page RPCs added in 086.
--
-- Both were SECURITY INVOKER reading base tables (images, listings,
-- scrape_runs). Base tables have RLS enabled with no anon policy, so
-- the browser (anon key) got zeros / empty sets even though the data
-- exists — the MCP verification passed only because it ran as the
-- service role. The fix follows the established frontend contract:
-- anon reads exclusively through the owner-privileged *_public views.
--
--   - image_storage_overview() now aggregates images_public +
--     listings_public (both already anon-granted) instead of the base
--     images / listings tables.
--   - scrape_runs has no public view yet; add scrape_runs_public
--     (every column is non-sensitive: counts, timestamps, category
--     JSON) and repoint recent_scrape_runs() at it.

create view scrape_runs_public as
  select
    id,
    started_at,
    ended_at,
    run_type,
    index_pages,
    listings_found_new,
    listings_scraped_new,
    listings_updated,
    listings_inactive,
    images_discovered,
    images_stored,
    errors,
    by_category
  from scrape_runs;

grant select on scrape_runs_public to anon;

create or replace function recent_scrape_runs(p_days int default 14)
returns setof scrape_runs
language sql
stable
security invoker
as $$
  select *
  from scrape_runs_public
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
    from listings_public l
    left join images_public i on i.sreality_id = l.sreality_id
    group by 1, 2
  )
  select jsonb_build_object(
    'total_images',  (select count(*) from images_public),
    'stored_images', (select count(*) from images_public where storage_path is not null),
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
