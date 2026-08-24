-- W10b (hydration sprint): price_stat_growth() computed st_asgeojson() for
-- every obec on EVERY call — including every operator drag of the [from,to]
-- window, even though a municipality's boundary polygon never changes.
-- Memory note: ~5.9 MB of window-invariant GeoJSON re-sent per window change,
-- one Datasets-page interaction and one Browse growth-overlay redraw at a time.
--
-- Split into two reads with different cache lifetimes:
--   - price_stat_growth_shapes(dataset_id): geometry only, keyed on dataset_id
--     alone (not from/to) — fetched once per dataset and cached forever
--     client-side. Obec universe = every obec that has EVER had an observation
--     for this dataset (ignores the window entirely, since a shape has to
--     cover any window the operator might pick, not just the current one).
--   - price_stat_growth(): unchanged numbers, geojson column dropped. The join
--     to admin_boundaries_public becomes an EXISTS check (same obec filter,
--     zero geometry computed) instead of an inner join that pulled geom.
create or replace function price_stat_growth_shapes(p_dataset_id bigint)
returns table(obec_id bigint, geojson text)
language sql stable as $function$
  select b.id as obec_id, st_asgeojson(b.geom::geometry, 5) as geojson
  from admin_boundaries_public b
  where b.id in (
    select distinct o.obec_id
    from price_stat_observations_public o
    where o.dataset_id = p_dataset_id and o.obec_id is not null
  );
$function$;

-- New function — this project's default privileges auto-GRANT; match the
-- sibling price_stat_growth's live ACL exactly (authenticated only, anon dark).
revoke all on function price_stat_growth_shapes(bigint) from public, anon;
grant execute on function price_stat_growth_shapes(bigint) to authenticated;

-- DROP + CREATE (not CREATE OR REPLACE) because the return columns change —
-- re-grants below restore the live ACL a plain DROP would otherwise reset to
-- the auto-GRANT default.
drop function if exists price_stat_growth(bigint, text, text);

create function price_stat_growth(
  p_dataset_id bigint,
  p_from text default null,
  p_to text default null
)
returns table(
  obec_id bigint, locality_name text,
  sale_latest_price integer, sale_cagr_pct double precision, sale_min_active integer,
  rent_latest_price integer, rent_cagr_pct double precision, rent_min_active integer,
  gross_yield_pct double precision, yield_change_pp_pa double precision
)
language sql stable as $function$
  with bounds as (
    select
      case when p_from is null then null
           else split_part(p_from, '-', 1)::int * 12
                + split_part(p_from, '-', 2)::int - 1 end as from_idx,
      case when p_to is null then null
           else split_part(p_to, '-', 1)::int * 12
                + split_part(p_to, '-', 2)::int - 1 end as to_idx
  ),
  obs as (
    select o.obec_id, o.locality_name, o.category_type_cb,
           (o.year * 12 + o.month - 1) as ymi, o.price, o.active_count
      from price_stat_observations_public o, bounds b
     where o.dataset_id = p_dataset_id
       and o.price is not null and o.price > 0
       and o.obec_id is not null
       and (b.from_idx is null or (o.year * 12 + o.month - 1) >= b.from_idx)
       and (b.to_idx is null or (o.year * 12 + o.month - 1) <= b.to_idx)
  ),
  agg as (
    select obec_id, max(locality_name) as locality_name, category_type_cb,
           min(ymi) as start_ymi, max(ymi) as end_ymi,
           least(
             (array_agg(active_count order by ymi))[1],
             (array_agg(active_count order by ymi desc))[1]
           ) as min_active,
           (array_agg(price order by ymi))[1] as start_price,
           (array_agg(price order by ymi desc))[1] as end_price
      from obs group by obec_id, category_type_cb
  ),
  piv as (
    select obec_id,
           max(locality_name) as locality_name,
           max(end_price)   filter (where category_type_cb = 1) as sale_end,
           max(start_price) filter (where category_type_cb = 1) as sale_start,
           max(end_ymi)     filter (where category_type_cb = 1) as sale_end_ymi,
           min(start_ymi)   filter (where category_type_cb = 1) as sale_start_ymi,
           max(min_active)  filter (where category_type_cb = 1) as sale_min_active,
           max(end_price)   filter (where category_type_cb = 2) as rent_end,
           max(start_price) filter (where category_type_cb = 2) as rent_start,
           max(end_ymi)     filter (where category_type_cb = 2) as rent_end_ymi,
           min(start_ymi)   filter (where category_type_cb = 2) as rent_start_ymi,
           max(min_active)  filter (where category_type_cb = 2) as rent_min_active
      from agg group by obec_id
  )
  select
    p.obec_id,
    p.locality_name,
    p.sale_end::int,
    case when p.sale_end_ymi - p.sale_start_ymi >= 12 and p.sale_start > 0
         then (power(p.sale_end::numeric / p.sale_start,
                     12.0 / (p.sale_end_ymi - p.sale_start_ymi)) - 1) * 100 end,
    p.sale_min_active::int,
    p.rent_end::int,
    case when p.rent_end_ymi - p.rent_start_ymi >= 12 and p.rent_start > 0
         then (power(p.rent_end::numeric / p.rent_start,
                     12.0 / (p.rent_end_ymi - p.rent_start_ymi)) - 1) * 100 end,
    p.rent_min_active::int,
    case when p.sale_end > 0 and p.rent_end is not null
         then 12.0 * p.rent_end / p.sale_end * 100 end,
    case when p.sale_end > 0 and p.sale_start > 0
              and p.rent_end is not null and p.rent_start is not null
              and greatest(p.sale_end_ymi, p.rent_end_ymi)
                  - least(p.sale_start_ymi, p.rent_start_ymi) >= 12
         then ((12.0 * p.rent_end / p.sale_end * 100)
               - (12.0 * p.rent_start / p.sale_start * 100))
              / ((greatest(p.sale_end_ymi, p.rent_end_ymi)
                  - least(p.sale_start_ymi, p.rent_start_ymi)) / 12.0) end
  from piv p
  where exists (select 1 from admin_boundaries_public b where b.id = p.obec_id);
$function$;

revoke all on function price_stat_growth(bigint, text, text) from public, anon;
grant execute on function price_stat_growth(bigint, text, text) to authenticated;
