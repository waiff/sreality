-- 069_browse_stats_district_context.sql
--
-- Each district chip can now carry a parent-municipality context so
-- picking "Edvarda Beneše" from the Mapy.cz dropdown's Plzeň entry
-- narrows the cohort to that city alone instead of returning every
-- street with the same name across the country (Plzeň + Olomouc +
-- Hradec Králové — 7 hits across 3 cities, the bug exposed by
-- migration 067).
--
-- The frontend now sends two parallel arrays: `districts_filter`
-- (chip names, unchanged) and `districts_context_filter` (the parent
-- municipality from Mapy.cz's `regionalStructure`, or NULL / '' for
-- picks at the municipality level and coarser). They walk in lockstep
-- via `unnest(..., ...) WITH ORDINALITY` so chip `i`'s context
-- narrows chip `i`'s name match.
--
-- Per-chip predicate:
--   (district ILIKE '%name%' OR locality ILIKE '%name%')
--   AND (ctx IS NULL OR ctx = '' OR
--        district ILIKE '%ctx%' OR locality ILIKE '%ctx%')
-- OR'd across chips. ctx = NULL or '' preserves the migration 067
-- behaviour exactly, so "okres Jihlava" / "Praha" / "Hruškové Dvory"
-- continue to match without narrowing.
--
-- Adding a parameter to a function in Postgres creates a NEW overload
-- alongside the existing one — `CREATE OR REPLACE FUNCTION` only
-- replaces when the parameter list is an exact match. Without an
-- explicit DROP of the 39-param overload, calls using named
-- arguments (the only call style we use) become ambiguous:
-- "function browse_stats(districts_filter => text[]) is not unique".
-- The DROP IF EXISTS below removes the 068 catch-up overload before
-- the new 40-param signature is created. Grants on the 40-param
-- version then come straight from the role-level
-- `GRANT EXECUTE ON FUNCTION ... TO anon` already on the database —
-- no re-grant needed.
--
-- Same performance profile as 067: ILIKE on `locality` is a
-- seq-scan at the current row count (tens of thousands) and runs
-- well under 100 ms. The added per-chip ctx check is one extra
-- ILIKE per row per chip — same magnitude. If row count crosses
-- ~250k, the follow-up flagged in 067 (pg_trgm + GIN trigram index)
-- becomes attractive.

drop function if exists browse_stats(
  text[], text[], integer, integer, integer, integer, boolean,
  integer, integer, integer, integer, integer, integer,
  boolean, boolean, boolean, boolean, text, boolean, boolean, boolean,
  integer, text[], bigint[], text, text,
  double precision, double precision, double precision, double precision,
  text, double precision, double precision, double precision,
  double precision, integer, double precision, double precision,
  text[]
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
  districts_context_filter text[]   default null
)
returns jsonb
language sql
stable
as $$
  with filtered as (
    select
      sreality_id, first_seen_at, last_seen_at, is_active,
      price_czk, area_m2, disposition, tom_days
    from listings_public
    where
          (not active_only_filter   or is_active = true)
      and (not inactive_only_filter or is_active = false)
      and (last_seen_max_days is null
           or last_seen_at >= now() - (last_seen_max_days || ' days')::interval)
      and (last_seen_min_days is null
           or last_seen_at <= now() - (last_seen_min_days || ' days')::interval)
      and (first_seen_max_days is null
           or first_seen_at >= now() - (first_seen_max_days || ' days')::interval)
      and (first_seen_min_days is null
           or first_seen_at <= now() - (first_seen_min_days || ' days')::interval)
      and (tom_days_min is null or tom_days >= tom_days_min)
      and (tom_days_max is null or tom_days <= tom_days_max)
      and (category_main_filter   is null or category_main   = category_main_filter)
      and (category_type_filter   is null or category_type   = category_type_filter)
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
          where (district ilike '%' || needle || '%'
              or locality ilike '%' || needle || '%')
            and (ctx is null or ctx = ''
              or district ilike '%' || ctx || '%'
              or locality ilike '%' || ctx || '%')
        )
      )
      and (dispositions_filter    is null or disposition     = any(dispositions_filter))
      and (price_min_filter       is null or price_czk      >= price_min_filter)
      and (price_max_filter       is null or price_czk      <= price_max_filter)
      and (area_min_filter        is null or area_m2        >= area_min_filter)
      and (area_max_filter        is null or area_m2        <= area_max_filter)
      and (has_balcony_filter     is null or has_balcony     = has_balcony_filter)
      and (has_lift_filter        is null or has_lift        = has_lift_filter)
      and (has_parking_filter     is null or has_parking     = has_parking_filter)
      and (furnished_filter       is null or furnished       = furnished_filter)
      and (terrace_filter         is null or terrace         = terrace_filter)
      and (cellar_filter          is null or cellar          = cellar_filter)
      and (garage_filter          is null or garage          = garage_filter)
      and (category_sub_cb_filter is null or category_sub_cb = category_sub_cb_filter)
      and (building_type_filter   is null or array_length(building_type_filter, 1) is null
           or building_type = any(building_type_filter))
      and (
        condition_match_filter is null
        or array_length(condition_match_filter, 1) is null
        or condition = any(condition_match_filter)
      )
      and (ownership_filter        is null or ownership      = ownership_filter)
      and (estate_area_min_filter  is null or estate_area   >= estate_area_min_filter)
      and (estate_area_max_filter  is null or estate_area   <= estate_area_max_filter)
      and (usable_area_min_filter  is null or usable_area   >= usable_area_min_filter)
      and (usable_area_max_filter  is null or usable_area   <= usable_area_max_filter)
      and (parking_lots_min_filter is null or parking_lots  >= parking_lots_min_filter)
      and (garden_area_min_filter  is null or garden_area   >= garden_area_min_filter)
      and (garden_area_max_filter  is null or garden_area   <= garden_area_max_filter)
      and (bbox_west  is null or lng >= bbox_west)
      and (bbox_east  is null or lng <= bbox_east)
      and (bbox_south is null or lat >= bbox_south)
      and (bbox_north is null or lat <= bbox_north)
      and (
        tag_ids is null
        or array_length(tag_ids, 1) is null
        or sreality_id in (
          select lt.sreality_id
          from listing_tags lt
          where lt.tag_id = any(tag_ids)
          group by lt.sreality_id
          having count(distinct lt.tag_id) = array_length(tag_ids, 1)
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
