-- 149_price_stat_series.sql
--
-- Per-obec monthly series for the map hover-chart: for a dataset + window,
-- the sale and rent price per (obec, year, month). The frontend derives the
-- displayed variable (rent price / sale price / 12·rent/sale gross yield) for
-- the active metric and a fixed Y domain across all obce, so the hover line
-- reads as one moving chart. SECURITY INVOKER over the anon-readable view.

create or replace function price_stat_series(
  p_dataset_id bigint,
  p_from text default null,
  p_to text default null
) returns table (
  obec_id    bigint,
  year       smallint,
  month      smallint,
  sale_price integer,
  rent_price integer
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
    select o.obec_id, o.year, o.month, o.category_type_cb, o.price
      from price_stat_observations_public o, bounds b
     where o.dataset_id = p_dataset_id
       and o.obec_id is not null
       and o.price is not null and o.price > 0
       and (b.from_idx is null or (o.year * 12 + o.month - 1) >= b.from_idx)
       and (b.to_idx is null or (o.year * 12 + o.month - 1) <= b.to_idx)
  )
  select obec_id, year, month,
         max(price) filter (where category_type_cb = 1)::int as sale_price,
         max(price) filter (where category_type_cb = 2)::int as rent_price
    from obs
   group by obec_id, year, month;
$$;

grant execute on function price_stat_series(bigint, text, text) to anon;
