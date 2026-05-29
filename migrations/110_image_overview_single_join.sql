-- 110_image_overview_single_join.sql
--
-- Fix a statement-timeout regression in image_storage_overview() introduced by
-- migration 109. The active-listing counts were computed by a SECOND full join
-- over the 1.3M-row images x listings set (the `active_totals` CTE), doubling
-- the query's cost and pushing it past the anon statement_timeout — the Image
-- mirror tile showed "canceling statement due to statement timeout".
--
-- per_cat already computes total_active / stored_active per category, so the
-- global active totals are just SUMs over per_cat — no second scan needed. Back
-- to one join, same shape as the pre-109 query that worked.

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
      count(i.id)                                       as total,
      count(i.storage_path)                             as stored,
      count(i.id) filter (where l.is_active)            as total_active,
      count(i.storage_path) filter (where l.is_active)  as stored_active
    from listings_public l
    left join images_public i on i.sreality_id = l.sreality_id
    group by 1, 2
  )
  select jsonb_build_object(
    'total_images',         (select count(*) from images_public),
    'stored_images',        (select count(*) from images_public where storage_path is not null),
    'total_active_images',  (select coalesce(sum(total_active), 0) from per_cat),
    'stored_active_images', (select coalesce(sum(stored_active), 0) from per_cat),
    'by_category', coalesce((
      select jsonb_agg(
        jsonb_build_object(
          'category_main', category_main,
          'category_type', category_type,
          'total',         total,
          'stored',        stored,
          'total_active',  total_active,
          'stored_active', stored_active
        )
        order by category_main, category_type
      )
      from per_cat
    ), '[]'::jsonb)
  )
$$;
grant execute on function image_storage_overview() to anon;
