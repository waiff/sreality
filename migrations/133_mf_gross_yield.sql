-- 133_mf_gross_yield.sql
--
-- MF gross rental yield per sale listing, for Browse filtering.
--
-- For every SALE apartment we know the asking price; the MF Cenová mapa
-- nájemného (migration 132) gives a reference monthly rent for that
-- apartment's territory + size category + amenities. The gross yield is
-- (reference_rent * 12) / asking_price * 100 — "what gross rental yield
-- would this for-sale flat throw off if let at the state's reference rent".
--
-- Stored on `listings` (two derived columns), exposed on the public views,
-- and filterable in Browse via min/max bounds. NULL where not computable:
-- non-apartments (the MF map is apartment rent only), rentals, missing
-- price/area/disposition/geom, or a territory the map doesn't cover.
--
-- Maintained by recompute_mf_gross_yields() — called after each rent-map
-- ingest and on a schedule (recompute_mf_yields.yml). NULL initially until
-- the first recompute runs; missing data, not an error.

alter table listings
  add column mf_reference_rent_czk integer,
  add column mf_gross_yield_pct    numeric;

comment on column listings.mf_gross_yield_pct is
  'Gross rental yield %: MF reference monthly rent * 12 / asking price * 100. '
  'Sale apartments only; NULL otherwise. Maintained by recompute_mf_gross_yields().';
comment on column listings.mf_reference_rent_czk is
  'MF Cenová mapa reference monthly rent (CZK) for this apartment. '
  'NULL when not computable. The numerator behind mf_gross_yield_pct.';

create index listings_mf_gross_yield_pct_idx
  on listings (mf_gross_yield_pct) where mf_gross_yield_pct is not null;


-- Recompute the two derived columns for every SALE listing in one set-based
-- pass. Apartments with a price/area/disposition/geom that resolve to an MF
-- territory get a value; every other sale listing is reset to NULL. The
-- `is distinct from` guard makes re-runs cheap (only changed rows written).
create or replace function public.recompute_mf_gross_yields()
returns integer
language plpgsql
as $function$
declare
  n integer;
begin
  with cand as (
    select
      l.sreality_id, l.category_main, l.geom, l.price_czk, l.area_m2,
      l.has_balcony, l.terrace, l.furnished, l.garage, l.has_lift,
      l.building_type,
      (l.condition = 'novostavba') as is_nov,
      case
        when l.disposition ~ '^[[:space:]]*[01]' then 1
        when l.disposition ~ '^[[:space:]]*2'    then 2
        when l.disposition ~ '^[[:space:]]*3'    then 3
        when l.disposition ~ '^[[:space:]]*[4-9]' then 4
        else null
      end as vk
    from listings l
    where l.category_type = 'prodej'
  ),
  matched as (
    select
      c.sreality_id, c.price_czk, c.area_m2, c.vk, c.is_nov,
      c.has_balcony, c.terrace, c.furnished, c.garage, c.has_lift,
      c.building_type,
      case when c.is_nov then v.ref_rent_novostavba_per_m2
           else v.ref_rent_per_m2 end as base
    from cand c
    join lateral (
      select b.id
      from admin_boundaries b
      where b.level in ('ku', 'obec')
        and st_covers(b.geom, c.geom)
      order by case b.level when 'ku' then 0 else 1 end
      limit 1
    ) terr on true
    join rent_map_values_public v
      on v.ruian_code = terr.id and v.vk = c.vk
    where c.category_main = 'byt'
      and c.vk is not null
      and c.geom is not null
      -- floor excludes placeholder ("cena v RK" = 1) and rent-magnitude
      -- prices mis-tagged as 'prodej'; no real apartment sells under 100k.
      and c.price_czk >= 100000
      and c.area_m2 is not null and c.area_m2 > 0
  ),
  adj as (
    select
      m.sreality_id,
      coalesce(sum(a.czk_per_m2) filter (where
           (a.attribute = 'balcony'   and m.has_balcony)
        or (a.attribute = 'terrace'   and m.terrace)
        or (a.attribute = 'furnished' and m.furnished = 'ano')
        or (a.attribute = 'garage'    and m.garage)
        or (a.attribute = 'elevator'  and m.has_lift)
        or (a.attribute = 'other_material' and m.is_nov
            and m.building_type is not null
            and m.building_type not in ('panel', 'cihla'))
      ), 0) as adj_sum
    from matched m
    join rent_map_adjustments_public a
      on a.vk = m.vk and a.is_novostavba = m.is_nov
    group by m.sreality_id
  ),
  computed as (
    select
      m.sreality_id,
      round((m.base + coalesce(a.adj_sum, 0)) * m.area_m2)::integer as rent_czk,
      round((m.base + coalesce(a.adj_sum, 0)) * m.area_m2 * 12
            / m.price_czk * 100, 2) as yield_pct
    from matched m
    left join adj a on a.sreality_id = m.sreality_id
    where m.base is not null
  ),
  final as (
    select c.sreality_id, comp.rent_czk, comp.yield_pct
    from cand c
    left join computed comp on comp.sreality_id = c.sreality_id
  )
  update listings l
    set mf_reference_rent_czk = f.rent_czk,
        mf_gross_yield_pct    = f.yield_pct
  from final f
  where l.sreality_id = f.sreality_id
    and (l.mf_reference_rent_czk is distinct from f.rent_czk
         or l.mf_gross_yield_pct is distinct from f.yield_pct);

  get diagnostics n = row_count;
  return n;
end;
$function$;


-- --- public views: expose the two new columns (trailing) -------------------
-- listings_public + properties_public reproduced verbatim from migration 126
-- with mf_reference_rent_czk / mf_gross_yield_pct appended.

create or replace view listings_public as
 SELECT sreality_id,
    first_seen_at,
    last_seen_at,
    is_active,
    category_main,
    category_type,
    price_czk,
    price_unit,
    area_m2,
    disposition,
    locality,
    district,
    locality_district_id,
    locality_region_id,
    st_y(geom::geometry) AS lat,
    st_x(geom::geometry) AS lng,
    floor,
    total_floors,
    has_balcony,
    has_parking,
    has_lift,
    building_type,
    condition,
    energy_rating,
    estate_area,
    usable_area,
    garden_area,
    category_sub_cb,
    furnished,
    terrace,
    cellar,
    garage,
    parking_lots,
    ownership,
    broker_name,
    broker_email,
    broker_phone,
        CASE
            WHEN is_active THEN GREATEST(0, floor(EXTRACT(epoch FROM now() - first_seen_at) / 86400::numeric)::integer)
            ELSE GREATEST(0, floor(EXTRACT(epoch FROM last_seen_at - first_seen_at) / 86400::numeric)::integer)
        END AS tom_days,
        CASE
            WHEN area_m2 IS NOT NULL AND area_m2 > 0::numeric AND price_czk IS NOT NULL THEN price_czk::numeric / area_m2::numeric
            ELSE NULL::numeric
        END AS price_per_m2,
    building_condition_level,
    apartment_condition_level,
    description,
    source,
    street,
    house_number,
    mf_reference_rent_czk,
    mf_gross_yield_pct
   FROM listings;

grant select on listings_public to anon;


create or replace view properties_public as
select
  p.id                          as property_id,
  p.repr_listing_id             as sreality_id,
  p.first_seen_at,
  p.last_seen_at,
  p.is_active,
  p.category_main,
  p.category_type,
  p.current_price_czk           as price_czk,
  l.price_unit,
  p.area_m2,
  p.disposition,
  p.locality,
  p.district,
  l.locality_district_id,
  l.locality_region_id,
  ST_Y(p.geom::geometry)        as lat,
  ST_X(p.geom::geometry)        as lng,
  l.floor,
  l.total_floors,
  p.has_balcony,
  p.has_parking,
  p.has_lift,
  p.building_type,
  p.condition,
  l.energy_rating,
  p.estate_area,
  p.usable_area,
  p.garden_area,
  p.category_sub_cb,
  p.furnished,
  p.terrace,
  p.cellar,
  p.garage,
  p.parking_lots,
  p.ownership,
  l.broker_name,
  l.broker_email,
  l.broker_phone,
  case
    when p.is_active then GREATEST(0, floor(EXTRACT(epoch FROM now() - p.first_seen_at) / 86400::numeric)::integer)
    else GREATEST(0, floor(EXTRACT(epoch FROM p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
  end                           as tom_days,
  case
    when p.area_m2 is not null and p.area_m2 > 0::numeric and p.current_price_czk is not null
      then p.current_price_czk::numeric / p.area_m2
    else null::numeric
  end                           as price_per_m2,
  l.building_condition_level,
  l.apartment_condition_level,
  l.description,
  p.source_count,
  p.distinct_site_count,
  p.price_drop_count,
  p.price_rise_count,
  p.max_price_drop_pct,
  p.stats_computed_at,
  l.source,
  l.street,
  l.mf_reference_rent_czk,
  l.mf_gross_yield_pct
from properties p
  left join listings l on l.sreality_id = p.repr_listing_id
where p.status = 'active'::text;

grant select on properties_public to anon;


-- --- browse_stats_properties: + mf_gross_yield_pct min/max -----------------
-- Reproduced verbatim from migration 118 with two new params + two WHERE
-- clauses so the Stats tab honours the yield filter like Map / Table.

drop function if exists public.browse_stats_properties(
  text[], text[], integer, integer, integer, integer, boolean, integer,
  integer, integer, integer, integer, integer, boolean, boolean, boolean,
  boolean, text, boolean, boolean, boolean, integer, text[], bigint[], text,
  text, double precision, double precision, double precision, double precision,
  text, double precision, double precision, double precision, double precision,
  integer, double precision, double precision, text[], text[], jsonb, integer,
  integer, jsonb, double precision, double precision, integer, integer,
  integer, double precision, text[]
);

create or replace function public.browse_stats_properties(
  districts_filter text[] default null::text[],
  dispositions_filter text[] default null::text[],
  price_min_filter integer default null::integer,
  price_max_filter integer default null::integer,
  area_min_filter integer default null::integer,
  area_max_filter integer default null::integer,
  active_only_filter boolean default false,
  last_seen_min_days integer default null::integer,
  last_seen_max_days integer default null::integer,
  first_seen_min_days integer default null::integer,
  first_seen_max_days integer default null::integer,
  tom_days_min integer default null::integer,
  tom_days_max integer default null::integer,
  has_balcony_filter boolean default null::boolean,
  has_lift_filter boolean default null::boolean,
  has_parking_filter boolean default null::boolean,
  inactive_only_filter boolean default false,
  furnished_filter text default null::text,
  terrace_filter boolean default null::boolean,
  cellar_filter boolean default null::boolean,
  garage_filter boolean default null::boolean,
  category_sub_cb_filter integer default null::integer,
  building_type_filter text[] default null::text[],
  tag_ids bigint[] default null::bigint[],
  category_main_filter text default null::text,
  category_type_filter text default null::text,
  bbox_west double precision default null::double precision,
  bbox_south double precision default null::double precision,
  bbox_east double precision default null::double precision,
  bbox_north double precision default null::double precision,
  ownership_filter text default null::text,
  estate_area_min_filter double precision default null::double precision,
  estate_area_max_filter double precision default null::double precision,
  usable_area_min_filter double precision default null::double precision,
  usable_area_max_filter double precision default null::double precision,
  parking_lots_min_filter integer default null::integer,
  garden_area_min_filter double precision default null::double precision,
  garden_area_max_filter double precision default null::double precision,
  condition_match_filter text[] default null::text[],
  districts_context_filter text[] default null::text[],
  city_index_rules jsonb default null::jsonb,
  city_pop_min integer default null::integer,
  city_pop_max integer default null::integer,
  city_proximity jsonb default null::jsonb,
  price_per_m2_min double precision default null::double precision,
  price_per_m2_max double precision default null::double precision,
  distinct_site_count_min integer default null::integer,
  price_drop_count_min integer default null::integer,
  price_rise_count_min integer default null::integer,
  max_price_drop_pct_min double precision default null::double precision,
  portal_filter text[] default null::text[],
  mf_gross_yield_pct_min double precision default null::double precision,
  mf_gross_yield_pct_max double precision default null::double precision
)
 returns jsonb
 language plpgsql
 stable
 set plan_cache_mode to 'force_custom_plan'
as $function$
begin
  return (
  with filtered as (
    select
      l.sreality_id, l.first_seen_at, l.last_seen_at, l.is_active,
      l.price_czk, l.area_m2, l.disposition, l.tom_days
    from properties_public l
    where
          (not active_only_filter   or l.is_active = true)
      and (not inactive_only_filter or l.is_active = false)
      and (last_seen_max_days is null
           or l.last_seen_at >= now() - (last_seen_max_days || ' days')::interval)
      and (last_seen_min_days is null
           or l.last_seen_at <= now() - (last_seen_min_days || ' days')::interval)
      and (first_seen_max_days is null
           or l.first_seen_at >= now() - (first_seen_max_days || ' days')::interval)
      and (first_seen_min_days is null
           or l.first_seen_at <= now() - (first_seen_min_days || ' days')::interval)
      and (tom_days_min is null or l.tom_days >= tom_days_min)
      and (tom_days_max is null or l.tom_days <= tom_days_max)
      and (category_main_filter   is null or l.category_main   = category_main_filter)
      and (category_type_filter   is null or l.category_type   = category_type_filter)
      and (
        districts_filter is null
        or array_length(districts_filter, 1) is null
        or exists (
          select 1
          from unnest(
                 districts_filter,
                 coalesce(
                   districts_context_filter,
                   array_fill(null::text, array[array_length(districts_filter, 1)])
                 )
               ) with ordinality as t(needle, ctx, ord)
          where (l.district ilike '%' || needle || '%'
              or l.locality ilike '%' || needle || '%')
            and (ctx is null or ctx = ''
              or l.district ilike '%' || ctx || '%'
              or l.locality ilike '%' || ctx || '%')
        )
      )
      and (dispositions_filter    is null or l.disposition     = any(dispositions_filter))
      and (price_min_filter       is null or l.price_czk      >= price_min_filter)
      and (price_max_filter       is null or l.price_czk      <= price_max_filter)
      and (area_min_filter        is null or l.area_m2        >= area_min_filter)
      and (area_max_filter        is null or l.area_m2        <= area_max_filter)
      and (price_per_m2_min is null
           or (l.area_m2 is not null and l.area_m2 > 0
               and l.price_czk::numeric / l.area_m2 >= price_per_m2_min))
      and (price_per_m2_max is null
           or (l.area_m2 is not null and l.area_m2 > 0
               and l.price_czk::numeric / l.area_m2 <= price_per_m2_max))
      and (mf_gross_yield_pct_min is null or l.mf_gross_yield_pct >= mf_gross_yield_pct_min)
      and (mf_gross_yield_pct_max is null or l.mf_gross_yield_pct <= mf_gross_yield_pct_max)
      and (has_balcony_filter     is null or l.has_balcony     = has_balcony_filter)
      and (has_lift_filter        is null or l.has_lift        = has_lift_filter)
      and (has_parking_filter     is null or l.has_parking     = has_parking_filter)
      and (furnished_filter       is null or l.furnished       = furnished_filter)
      and (terrace_filter         is null or l.terrace         = terrace_filter)
      and (cellar_filter          is null or l.cellar          = cellar_filter)
      and (garage_filter          is null or l.garage          = garage_filter)
      and (category_sub_cb_filter is null or l.category_sub_cb = category_sub_cb_filter)
      and (building_type_filter   is null or array_length(building_type_filter, 1) is null
           or l.building_type = any(building_type_filter))
      and (
        condition_match_filter is null
        or array_length(condition_match_filter, 1) is null
        or l.condition = any(condition_match_filter)
      )
      and (portal_filter is null
           or array_length(portal_filter, 1) is null
           or l.source = any(portal_filter))
      and (ownership_filter        is null or l.ownership      = ownership_filter)
      and (estate_area_min_filter  is null or l.estate_area   >= estate_area_min_filter)
      and (estate_area_max_filter  is null or l.estate_area   <= estate_area_max_filter)
      and (usable_area_min_filter  is null or l.usable_area   >= usable_area_min_filter)
      and (usable_area_max_filter  is null or l.usable_area   <= usable_area_max_filter)
      and (parking_lots_min_filter is null or l.parking_lots  >= parking_lots_min_filter)
      and (garden_area_min_filter  is null or l.garden_area   >= garden_area_min_filter)
      and (garden_area_max_filter  is null or l.garden_area   <= garden_area_max_filter)
      and (bbox_west  is null or l.lng >= bbox_west)
      and (bbox_east  is null or l.lng <= bbox_east)
      and (bbox_south is null or l.lat >= bbox_south)
      and (bbox_north is null or l.lat <= bbox_north)
      and (distinct_site_count_min is null or l.distinct_site_count >= distinct_site_count_min)
      and (price_drop_count_min    is null or l.price_drop_count    >= price_drop_count_min)
      and (price_rise_count_min    is null or l.price_rise_count    >= price_rise_count_min)
      and (max_price_drop_pct_min  is null or l.max_price_drop_pct  >= max_price_drop_pct_min)
      and (
        tag_ids is null
        or array_length(tag_ids, 1) is null
        or l.sreality_id in (
          select lt.sreality_id
          from listing_tags lt
          where lt.tag_id = any(tag_ids)
          group by lt.sreality_id
          having count(distinct lt.tag_id) = array_length(tag_ids, 1)
        )
      )
      and (
        (
          (city_index_rules is null or jsonb_array_length(city_index_rules) = 0)
          and city_pop_min is null
          and city_pop_max is null
        )
        or (
          l.lat is not null and l.lng is not null
          and exists (
            select 1 from curated_cities_public c
            where st_dwithin(
                    st_setsrid(st_makepoint(l.lng, l.lat), 4326)::geography,
                    st_setsrid(st_makepoint(c.lng, c.lat), 4326)::geography,
                    c.default_radius_m)
              and (city_pop_min is null or c.population >= city_pop_min)
              and (city_pop_max is null or c.population <= city_pop_max)
              and not exists (
                select 1 from jsonb_array_elements(coalesce(city_index_rules, '[]'::jsonb)) r
                where not exists (
                  select 1 from city_index_values_public v
                  where v.city_id = c.city_id
                    and v.index_name = r->>'index_name'
                    and v.value >= (r->>'value')::numeric
                )
              )
          )
        )
      )
      and (
        city_proximity is null
        or (
          l.lat is not null and l.lng is not null
          and exists (
            select 1 from curated_cities_public c
            where st_dwithin(
                    st_setsrid(st_makepoint(l.lng, l.lat), 4326)::geography,
                    st_setsrid(st_makepoint(c.lng, c.lat), 4326)::geography,
                    ((city_proximity ->> 'radius_km')::int * 1000))
              and ((city_proximity ->> 'population_min')::int is null
                   or c.population >= (city_proximity ->> 'population_min')::int)
              and not exists (
                select 1 from jsonb_array_elements(
                  coalesce(city_proximity -> 'index_rules', '[]'::jsonb)
                ) r
                where not exists (
                  select 1 from city_index_values_public v
                  where v.city_id = c.city_id
                    and v.index_name = r->>'index_name'
                    and v.value >= (r->>'value')::numeric
                )
              )
          )
        )
      )
  ),
  price_pct as (
    select
      percentile_cont(0.25) within group (order by price_czk)::int as p25,
      percentile_cont(0.50) within group (order by price_czk)::int as p50,
      percentile_cont(0.75) within group (order by price_czk)::int as p75
    from filtered
    where price_czk is not null
  ),
  ppm2_pct as (
    select
      percentile_cont(0.25) within group (order by price_czk::numeric / area_m2)::int as p25,
      percentile_cont(0.50) within group (order by price_czk::numeric / area_m2)::int as p50,
      percentile_cont(0.75) within group (order by price_czk::numeric / area_m2)::int as p75
    from filtered
    where price_czk is not null and area_m2 is not null and area_m2 > 0
  ),
  disposition_dist as (
    select
      coalesce(disposition, 'unspecified') as disposition,
      count(*)::int as n,
      count(price_czk::numeric / nullif(area_m2, 0))::int as ppm2_n,
      min(price_czk::numeric / nullif(area_m2, 0))::int as ppm2_min,
      percentile_cont(0.25) within group (
        order by price_czk::numeric / nullif(area_m2, 0)
      )::int as ppm2_p25,
      percentile_cont(0.50) within group (
        order by price_czk::numeric / nullif(area_m2, 0)
      )::int as ppm2_median,
      percentile_cont(0.75) within group (
        order by price_czk::numeric / nullif(area_m2, 0)
      )::int as ppm2_p75,
      max(price_czk::numeric / nullif(area_m2, 0))::int as ppm2_max
    from filtered
    group by disposition
    order by n desc, disposition asc
  ),
  price_cuts as (
    select
      percentile_cont(0.10) within group (order by price_czk) as cut_10,
      percentile_cont(0.25) within group (order by price_czk) as cut_25,
      percentile_cont(0.45) within group (order by price_czk) as cut_45,
      percentile_cont(0.55) within group (order by price_czk) as cut_55,
      percentile_cont(0.75) within group (order by price_czk) as cut_75,
      percentile_cont(0.90) within group (order by price_czk) as cut_90,
      count(*)::int                                           as priced_total
    from filtered
    where price_czk is not null
  ),
  price_bands as (
    select
      f.price_czk,
      f.tom_days,
      case
        when f.price_czk <= c.cut_10 then 1
        when f.price_czk <= c.cut_25 then 2
        when f.price_czk <= c.cut_45 then 3
        when f.price_czk <= c.cut_55 then 4
        when f.price_czk <= c.cut_75 then 5
        when f.price_czk <= c.cut_90 then 6
        else                              7
      end                          as bucket,
      c.priced_total
    from filtered f, price_cuts c
    where f.price_czk is not null
  ),
  band_definitions(bucket, p_lo, p_hi) as (
    values (1, 0, 10), (2, 10, 25), (3, 25, 45), (4, 45, 55),
           (5, 55, 75), (6, 75, 90), (7, 90, 100)
  ),
  band_stats as (
    select
      d.bucket,
      d.p_lo,
      d.p_hi,
      count(b.price_czk)::int                                            as n,
      max(b.priced_total)                                                as priced_total,
      min(b.price_czk)::int                                              as price_min,
      max(b.price_czk)::int                                              as price_max,
      count(b.tom_days)::int                                             as tom_n,
      min(b.tom_days)::int                                               as tom_min,
      percentile_cont(0.25) within group (order by b.tom_days)
        filter (where b.tom_days is not null)                            as tom_p25,
      percentile_cont(0.50) within group (order by b.tom_days)
        filter (where b.tom_days is not null)                            as tom_median,
      percentile_cont(0.75) within group (order by b.tom_days)
        filter (where b.tom_days is not null)                            as tom_p75,
      max(b.tom_days)::int                                               as tom_max,
      avg(b.tom_days) filter (where b.tom_days is not null)              as tom_mean
    from band_definitions d
    left join price_bands b on b.bucket = d.bucket
    group by d.bucket, d.p_lo, d.p_hi
    order by d.bucket
  )
  select jsonb_build_object(
    'total',        (select count(*)::int from filtered),
    'new_7d',       (select count(*)::int from filtered where first_seen_at >= now() - interval '7 days'),
    'new_30d',      (select count(*)::int from filtered where first_seen_at >= now() - interval '30 days'),
    'price',        (select case when p50 is null then null
                                  else jsonb_build_object('p25', p25, 'p50', p50, 'p75', p75) end
                     from price_pct),
    'ppm2',         (select case when p50 is null then null
                                  else jsonb_build_object('p25', p25, 'p50', p50, 'p75', p75) end
                     from ppm2_pct),
    'dispositions', coalesce(
                      (select jsonb_agg(jsonb_build_object(
                          'disposition', disposition,
                          'n',           n,
                          'ppm2_box',    case when ppm2_n > 0
                                              then jsonb_build_object(
                                                'n',      ppm2_n,
                                                'min',    ppm2_min,
                                                'p25',    ppm2_p25,
                                                'median', ppm2_median,
                                                'p75',    ppm2_p75,
                                                'max',    ppm2_max
                                              )
                                              else null end
                        ))
                       from disposition_dist),
                      '[]'::jsonb
                    ),
    'price_band_velocity', coalesce(
                      (select jsonb_agg(jsonb_build_object(
                          'bucket',     bs.bucket,
                          'p_lo',       bs.p_lo,
                          'p_hi',       bs.p_hi,
                          'n',          bs.n,
                          'pct_share',  case when bs.priced_total is null or bs.priced_total = 0
                                             then null
                                             else round(bs.n * 100.0 / bs.priced_total, 1)
                                        end,
                          'price_min',  bs.price_min,
                          'price_max',  bs.price_max,
                          'tom_box',    case when bs.tom_n > 0
                                             then jsonb_build_object(
                                               'n',      bs.tom_n,
                                               'min',    bs.tom_min,
                                               'p25',    round(bs.tom_p25::numeric, 1),
                                               'median', round(bs.tom_median::numeric, 1),
                                               'mean',   round(bs.tom_mean::numeric, 1),
                                               'p75',    round(bs.tom_p75::numeric, 1),
                                               'max',    bs.tom_max
                                             )
                                             else null end
                        ) order by bs.p_lo)
                       from band_stats bs),
                      '[]'::jsonb
                    )
  )
  );
end
$function$;
