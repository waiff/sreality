-- 147_price_stat_config.sql
--
-- Per-dataset configuration + an interactive growth RPC for the price-stats
-- (ceny-nemovitosti) datasets:
--   * datasets gain a SCRAPE window (start_ym/end_ym) and a city selection
--     (obec_ids + population bounds) so each dataset/run targets its own set
--     of municipalities and date range (replaces the global cities JSON).
--   * price_stat_obce_picker_public: the kraj→okres→obec tree (id, parent_id,
--     population, sreality_id) the UI picker reads — no geometry, lightweight.
--     Obce are limited to those with a sreality_id (the scrapeable ones; all
--     275 obce ≥5k population have one — see the obec↔sreality_id note).
--   * price_stat_growth(dataset, from, to): per-obec sale/rent CAGR + gross
--     yield + yield CHANGE over ANY [from,to] window, computed live from the
--     observations (no re-scrape). Drives the Datasets analysis + the Browse
--     map overlay. SECURITY INVOKER over the anon-readable public views.

set local lock_timeout = '5s';

alter table price_stat_datasets
  add column start_ym        text,
  add column end_ym          text,
  add column obec_ids        bigint[],
  add column min_population   integer,
  add column max_population   integer;


-- Recreate the public view with the new config columns appended.
create or replace view price_stat_datasets_public as
  select id, slug, name, description, category_main_cb, building_condition,
         building_type, ownership, usable_area_from, usable_area_to, distance,
         is_active, created_at, updated_at,
         start_ym, end_ym, obec_ids, min_population, max_population
    from price_stat_datasets
   where is_active;


-- Kraj/okres/obec tree for the city picker (no geom → cheap to ship + cache).
create view price_stat_obce_picker_public as
  select id, level, name, parent_id, population, sreality_id
    from admin_boundaries
   where level in ('kraj', 'okres')
      or (level = 'obec' and sreality_id is not null);

grant select on price_stat_obce_picker_public to anon;


-- Per-obec growth over an arbitrary [p_from, p_to] window (YYYY-MM, inclusive;
-- NULL = open end). CAGR needs >= 1 year of usable span or it returns NULL
-- (a few-month ratio annualizes into nonsense). yield_change_pp_pa is the
-- annualized change in gross yield (percentage points/yr; negative = the
-- compression "pokles yield" story).
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
           min(active_count) as min_active,
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
