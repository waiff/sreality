-- 081_curated_cities_polygon.sql
--
-- Phase QUAL — wire curated_cities to admin_boundaries (RÚIAN obec
-- polygons, ingested by migration 017) and unlock per-rule `op` in
-- city-quality filters.
--
-- Two motivations, one migration:
--
-- 1. Polygon containment. The 078 model placed a centroid + per-city
--    `default_radius_m` (clamped to ≥ 2 km) around every curated city.
--    For physically large cities (Brno, Ostrava, Plzeň) the 2 km
--    floor silently excluded most of the city's listings from any
--    city-quality filter — anything in an outer district fell outside
--    the disc. Real obec polygons are already in the DB; we just
--    never wired curated_cities to them.
--
-- 2. Per-rule `op`. The frontend `CityIndexRule` wire model and
--    `_index_rule_predicate` in `toolkit/comparables.py` already
--    honour `op ∈ {>=, <=, ==, !=, >, <}`. The two RPCs hardcoded
--    `>=` in SQL, so the operator couldn't filter for "safety ≤ 3"
--    from Browse / Stats / Watchdog without per-call Python plumbing.
--
-- Schema delta:
--   curated_cities.admin_boundary_id  →  references admin_boundaries(id)
--   curated_cities_public             →  exposes admin_boundary_id
--   listings_with_city_quality        →  polygon-or-radius + op switch
--   browse_stats                      →  same predicate transform
--
-- Backfill: per-city polygon lookup walks obec → okres → kraj parent
-- chain and matches by lowercased name. Unmatched curated_cities keep
-- `admin_boundary_id = NULL` and fall back to the existing centroid +
-- radius semantics, so the migration is safe even if RÚIAN naming
-- drifts. Final `RAISE NOTICE` reports the unmatched count for the
-- operator.
--
-- Predicate shape (both RPCs, per-city block only — proximity stays
-- on radius because "within X km of a city" is inherently centroid-
-- around):
--   (c.admin_boundary_id IS NOT NULL AND ST_Covers(b.geom, point))
--   OR (c.admin_boundary_id IS NULL AND ST_DWithin(point, centroid,
--                                                  default_radius_m))
--
-- `ST_Covers(geography, geography)` is overloaded natively in
-- PostGIS — no `::geometry` casts — and uses the GiST index on
-- `admin_boundaries.geom`. Same predicate `scripts/ingest_boundaries.py`
-- already uses to populate `admin_boundaries.sreality_id`.

set local lock_timeout = '5s';


-- ---------------------------------------------------------------- column

alter table curated_cities
  add column admin_boundary_id bigint
    references admin_boundaries(id) on delete set null;

create index curated_cities_admin_boundary_idx
  on curated_cities (admin_boundary_id);


-- ---------------------------------------------------------------- view

-- `CREATE OR REPLACE VIEW` only permits *appending* columns at the
-- end of the SELECT list; we cannot insert `admin_boundary_id` next
-- to the radius. Keeping the historical column order intact and
-- adding the new column at the tail lets every existing reader
-- continue to bind by name without surprises.
create or replace view curated_cities_public as
  select
    c.id                              as city_id,
    c.name,
    c.kraj_name,
    st_y(c.centroid::geometry)        as lat,
    st_x(c.centroid::geometry)        as lng,
    c.default_radius_m,
    p.population,
    p.as_of_year                      as population_as_of_year,
    c.admin_boundary_id
  from curated_cities c
  left join lateral (
    select population, as_of_year
      from city_population
     where city_id = c.id
     order by as_of_year desc
     limit 1
  ) p on true;

grant select on curated_cities_public to anon;


-- ---------------------------------------------------------------- backfill

update curated_cities c
   set admin_boundary_id = obec.id
  from admin_boundaries obec
  join admin_boundaries okres
    on okres.id = obec.parent_id and okres.level = 'okres'
  join admin_boundaries kraj
    on kraj.id  = okres.parent_id and kraj.level = 'kraj'
 where c.admin_boundary_id is null
   and obec.level = 'obec'
   and lower(obec.name) = lower(c.name)
   and lower(kraj.name) = lower(c.kraj_name);

do $$
declare
  miss   int;
  total  int;
begin
  select count(*) into total from curated_cities;
  select count(*) into miss
    from curated_cities where admin_boundary_id is null;
  raise notice
    'curated_cities polygon-linked: %/%; unmatched: % (radius fallback)',
    total - miss, total, miss;
end $$;


-- ---------------------------------------------------------------- RPC: listings_with_city_quality

create or replace function listings_with_city_quality(
  p_index_rules jsonb default null,
  p_pop_min int default null,
  p_pop_max int default null,
  p_proximity jsonb default null
)
returns table (sreality_id bigint)
language sql
stable
security invoker
as $$
  with
    rules as (
      select
        (r->>'index_name')::text       as index_name,
        (r->>'value')::numeric         as value,
        coalesce(r->>'op', '>=')       as op
      from jsonb_array_elements(coalesce(p_index_rules, '[]'::jsonb)) r
    ),
    prox_rules as (
      select
        (r->>'index_name')::text       as index_name,
        (r->>'value')::numeric         as value,
        coalesce(r->>'op', '>=')       as op
      from jsonb_array_elements(
        coalesce(p_proximity -> 'index_rules', '[]'::jsonb)
      ) r
    )
  select l.sreality_id
  from listings l
  where
    l.geom is not null
    -- Per-city block: polygon containment when admin_boundary_id is
    -- set, centroid+radius fallback otherwise. Inner rule op comes
    -- from r.op; defaults to >= for back-compat with pre-081 callers.
    and (
      not (exists (select 1 from rules)
           or p_pop_min is not null
           or p_pop_max is not null)
      or exists (
        select 1
          from curated_cities_public c
          left join admin_boundaries_public b
            on b.id = c.admin_boundary_id
         where (
                 (c.admin_boundary_id is not null
                    and st_covers(b.geom, l.geom))
                 or (c.admin_boundary_id is null
                    and st_dwithin(
                          l.geom,
                          st_setsrid(st_makepoint(c.lng, c.lat), 4326)::geography,
                          c.default_radius_m))
               )
           and (p_pop_min is null or c.population >= p_pop_min)
           and (p_pop_max is null or c.population <= p_pop_max)
           and not exists (
             select 1 from rules r
             where not exists (
               select 1 from city_index_values_public v
               where v.city_id = c.city_id
                 and v.index_name = r.index_name
                 and case r.op
                       when '>=' then v.value >= r.value
                       when '<=' then v.value <= r.value
                       when '>'  then v.value >  r.value
                       when '<'  then v.value <  r.value
                       when '==' then v.value =  r.value
                       when '!=' then v.value <> r.value
                       else           v.value >= r.value
                     end
             )
           )
      )
    )
    -- Proximity block: "within radius_km of any matching city" is
    -- circle-around-centroid by definition; the polygon fast-path
    -- doesn't apply here. Only the op switch is new.
    and (
      p_proximity is null
      or exists (
        select 1
          from curated_cities_public c
         where st_dwithin(
                 l.geom,
                 st_setsrid(st_makepoint(c.lng, c.lat), 4326)::geography,
                 ((p_proximity ->> 'radius_km')::int * 1000)
               )
           and (
             (p_proximity ->> 'population_min')::int is null
             or c.population >= (p_proximity ->> 'population_min')::int
           )
           and not exists (
             select 1 from prox_rules r
             where not exists (
               select 1 from city_index_values_public v
               where v.city_id = c.city_id
                 and v.index_name = r.index_name
                 and case r.op
                       when '>=' then v.value >= r.value
                       when '<=' then v.value <= r.value
                       when '>'  then v.value >  r.value
                       when '<'  then v.value <  r.value
                       when '==' then v.value =  r.value
                       when '!=' then v.value <> r.value
                       else           v.value >= r.value
                     end
             )
           )
      )
    );
$$;

grant execute on function listings_with_city_quality(jsonb, int, int, jsonb)
  to anon, authenticated;


-- ---------------------------------------------------------------- RPC: browse_stats

drop function if exists browse_stats(
  text[], text[], integer, integer, integer, integer, boolean,
  integer, integer, integer, integer, integer, integer,
  boolean, boolean, boolean, boolean, text, boolean, boolean, boolean,
  integer, text[], bigint[], text, text,
  double precision, double precision, double precision, double precision,
  text, double precision, double precision, double precision,
  double precision, integer, double precision, double precision,
  text[], text[], jsonb, integer, integer, jsonb
);

create or replace function browse_stats(
  districts_filter         text[]   default null,
  dispositions_filter      text[]   default null,
  price_min_filter         integer  default null,
  price_max_filter         integer  default null,
  area_min_filter          integer  default null,
  area_max_filter          integer  default null,
  active_only_filter       boolean  default false,
  last_seen_min_days       integer  default null,
  last_seen_max_days       integer  default null,
  first_seen_min_days      integer  default null,
  first_seen_max_days      integer  default null,
  tom_days_min             integer  default null,
  tom_days_max             integer  default null,
  has_balcony_filter       boolean  default null,
  has_lift_filter          boolean  default null,
  has_parking_filter       boolean  default null,
  inactive_only_filter     boolean  default false,
  furnished_filter         text     default null,
  terrace_filter           boolean  default null,
  cellar_filter            boolean  default null,
  garage_filter            boolean  default null,
  category_sub_cb_filter   integer  default null,
  building_type_filter     text[]   default null,
  tag_ids                  bigint[] default null,
  category_main_filter     text     default null,
  category_type_filter     text     default null,
  bbox_west                double precision default null,
  bbox_south               double precision default null,
  bbox_east                double precision default null,
  bbox_north               double precision default null,
  ownership_filter         text     default null,
  estate_area_min_filter   double precision default null,
  estate_area_max_filter   double precision default null,
  usable_area_min_filter   double precision default null,
  usable_area_max_filter   double precision default null,
  parking_lots_min_filter  integer  default null,
  garden_area_min_filter   double precision default null,
  garden_area_max_filter   double precision default null,
  condition_match_filter   text[]   default null,
  districts_context_filter text[]   default null,
  city_index_rules         jsonb    default null,
  city_pop_min             integer  default null,
  city_pop_max             integer  default null,
  city_proximity           jsonb    default null
)
returns jsonb
language sql
stable
as $$
  with filtered as (
    select
      l.sreality_id, l.first_seen_at, l.last_seen_at, l.is_active,
      l.price_czk, l.area_m2, l.disposition, l.tom_days
    from listings_public l
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
      -- Phase QUAL: per-city block (index rules + population bounds).
      -- The l.lat / l.lng qualifiers below are LOAD-BEARING — without
      -- the alias the inner EXISTS would bind bare `lat`/`lng` to
      -- `curated_cities_public c`, which has identically-named columns.
      -- Polygon containment uses admin_boundaries_public when wired;
      -- centroid+radius is the fallback for curated_cities still
      -- lacking an admin_boundary_id (see migration 081).
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
            left join admin_boundaries_public b
              on b.id = c.admin_boundary_id
            where (
                    (c.admin_boundary_id is not null
                       and st_covers(
                             b.geom,
                             st_setsrid(st_makepoint(l.lng, l.lat), 4326)::geography))
                    or (c.admin_boundary_id is null
                       and st_dwithin(
                             st_setsrid(st_makepoint(l.lng, l.lat), 4326)::geography,
                             st_setsrid(st_makepoint(c.lng, c.lat), 4326)::geography,
                             c.default_radius_m))
                  )
              and (city_pop_min is null or c.population >= city_pop_min)
              and (city_pop_max is null or c.population <= city_pop_max)
              and not exists (
                select 1 from jsonb_array_elements(coalesce(city_index_rules, '[]'::jsonb)) r
                where not exists (
                  select 1 from city_index_values_public v
                  where v.city_id = c.city_id
                    and v.index_name = r->>'index_name'
                    and case coalesce(r->>'op', '>=')
                          when '>=' then v.value >= (r->>'value')::numeric
                          when '<=' then v.value <= (r->>'value')::numeric
                          when '>'  then v.value >  (r->>'value')::numeric
                          when '<'  then v.value <  (r->>'value')::numeric
                          when '==' then v.value =  (r->>'value')::numeric
                          when '!=' then v.value <> (r->>'value')::numeric
                          else           v.value >= (r->>'value')::numeric
                        end
                )
              )
          )
        )
      )
      -- Phase QUAL: proximity block. Same alias discipline. Stays on
      -- centroid+radius because radius_km IS the operator's input.
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
                    and case coalesce(r->>'op', '>=')
                          when '>=' then v.value >= (r->>'value')::numeric
                          when '<=' then v.value <= (r->>'value')::numeric
                          when '>'  then v.value >  (r->>'value')::numeric
                          when '<'  then v.value <  (r->>'value')::numeric
                          when '==' then v.value =  (r->>'value')::numeric
                          when '!=' then v.value <> (r->>'value')::numeric
                          else           v.value >= (r->>'value')::numeric
                        end
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
  );
$$;
