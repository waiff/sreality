-- 115_image_overview_matview.sql
--
-- Back the Health "Image mirror" tile with a materialized view and rewrite
-- image_storage_overview() to read from it.
--
-- Why: the tile's RPC aggregated the full images table (1.7M rows / 311MB heap,
-- and only growing) on every browser load. Migration 109 made it worse (a
-- second full join); migration 110 cut that back to one scan, but warm it is
-- still ~1.3s and cold-cache it blows past the anon role's 3s statement_timeout
-- — the tile showed "canceling statement due to statement timeout". A
-- full-table scan on every page load is the wrong shape for a dashboard stat.
--
-- So precompute the per-category rollup into a matview that a scheduled job
-- refreshes OFF the request path (scripts/refresh_image_stats.py, wired into
-- images.yml — the 2-hourly image backlog drain). The RPC now reads 12 rows
-- (microseconds) and assembles the SAME jsonb shape, so the frontend contract
-- is unchanged. REFRESH ... CONCURRENTLY (needs the unique index below) never
-- blocks anon readers, and a couple-hours-stale storage count is fine here.

create materialized view if not exists image_storage_overview_mv as
  select
    l.category_main,
    l.category_type,
    count(i.id)                                       as total,
    count(i.storage_path)                             as stored,
    count(i.id) filter (where l.is_active)            as total_active,
    count(i.storage_path) filter (where l.is_active)  as stored_active
  from listings_public l
  left join images_public i on i.sreality_id = l.sreality_id
  group by 1, 2;

-- Required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
create unique index if not exists image_storage_overview_mv_cat
  on image_storage_overview_mv (category_main, category_type);

grant select on image_storage_overview_mv to anon;

create or replace function image_storage_overview()
returns jsonb
language sql
stable
security invoker
as $$
  select jsonb_build_object(
    'total_images',         coalesce(sum(total), 0),
    'stored_images',        coalesce(sum(stored), 0),
    'total_active_images',  coalesce(sum(total_active), 0),
    'stored_active_images', coalesce(sum(stored_active), 0),
    'by_category', coalesce(
      jsonb_agg(
        jsonb_build_object(
          'category_main', category_main,
          'category_type', category_type,
          'total',         total,
          'stored',        stored,
          'total_active',  total_active,
          'stored_active', stored_active
        )
        order by category_main, category_type
      ),
      '[]'::jsonb)
  )
  from image_storage_overview_mv;
$$;
grant execute on function image_storage_overview() to anon;
