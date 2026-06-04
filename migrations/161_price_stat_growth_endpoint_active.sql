-- 161_price_stat_growth_endpoint_active.sql
--
-- Fix the price_stat_growth sparsity measure. A CAGR depends only on the FIRST
-- and LAST price in the window, so the listing-count reliability gate should
-- reflect those two endpoints — not min(active_count) over the whole series,
-- which let a single thin month (typically the sparse 2015 start) zero out an
-- otherwise-solid multi-year trend. With the endpoint measure, sale/rent
-- _min_active now means "the smaller of the start-point and end-point active
-- counts" — exactly the two observations the growth is built from.
--
-- The UI consumes this as a three-tier signal (see growthChoropleth.ts):
--   >= 3 at both endpoints → confident (full colour)
--   1..2 at an endpoint    → thin (faded tint, "data exists but limited")
--   0 / no CAGR            → no data (grey)
--
-- Signature unchanged → CREATE OR REPLACE only; nothing downstream re-types.

set local lock_timeout = '5s';

create or replace function price_stat_growth(
  p_dataset_id bigint,
  p_from text default null,
  p_to text default null
) returns table (
  obec_id            bigint,
  locality_name      text,
  geojson            text,
  sale_latest_price  integer,
  sale_cagr_pct      double precision,
  sale_min_active    integer,
  rent_latest_price  integer,
  rent_cagr_pct      double precision,
  rent_min_active    integer,
  gross_yield_pct    double precision,
  yield_change_pp_pa double precision
)
language sql
security invoker
stable
as $$
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
           -- reliability of the two endpoints the CAGR is built from
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
    st_asgeojson(b.geom::geometry, 5) as geojson,
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
  join admin_boundaries_public b on b.id = p.obec_id;
$$;

grant execute on function price_stat_growth(bigint, text, text) to anon;
