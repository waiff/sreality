-- 198_properties_mf_denorm_keyset_indexes.sql
--
-- Foundation for keyset-paginated infinite scroll on Browse (cards + table
-- read properties_public, ordered by a user-selectable sort column + the
-- property_id tiebreaker). Two changes, both additive:
--
-- 1. DENORMALISE mf_gross_yield_pct + mf_reference_rent_czk onto properties.
--    They lived only on `listings` and properties_public read them through
--    the LEFT JOIN. That join makes the yield SORT lane join-before-filter:
--    keyset-ordering on a listings column forces a full nested-loop join of
--    the whole cohort (measured ~1.9s on byt+prodej, >3s unfiltered — over
--    the anon 3s timeout). Every OTHER Browse sort column is properties-
--    sourced and keyset-cheap; this collapses the one exception back into
--    that class and also speeds the existing min/max_mf_gross_yield filter.
--    recompute_mf_gross_yields() (hourly + post rent-map ingest, the single
--    writer of the listings figures) now propagates them to the property's
--    representative-listing row. A repr change lags one mf-recompute cycle
--    (bounded, same spirit as rule #20's documented mirror lags).
--
-- 2. COMPOSITE KEYSET INDEXES (col, id) on the hot sort lanes. Keyset is
--    already correct + ~120ms unindexed, but these turn the per-page
--    full-cohort sort into an index range scan (sub-10ms) and keep it flat
--    as the table grows past 300k. (col, id) ascending serves BOTH scroll
--    directions via forward/backward scan (the keyset tiebreaker follows
--    the sort direction, so the ORDER BY is (col, id) same-direction).

-- --- 1a. columns -----------------------------------------------------------
alter table properties
  add column if not exists mf_reference_rent_czk integer,
  add column if not exists mf_gross_yield_pct    numeric;

comment on column properties.mf_gross_yield_pct is
  'Denormalised from the representative listing (migration 198) so Browse can '
  'keyset-sort/filter without the listings join. Maintained by '
  'recompute_mf_gross_yields().';
comment on column properties.mf_reference_rent_czk is
  'Denormalised from the representative listing (migration 198). The '
  'numerator behind mf_gross_yield_pct.';

-- --- 1b. recompute_mf_gross_yields(): + property sync -----------------------
-- Reproduced from migration 134 (the live definition, incl. the
-- mf_reference_rent jsonb breakdown) with one appended step: after writing
-- the per-listing figures, mirror them onto each property's representative
-- listing. `n` still returns the count of changed LISTINGS rows.
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
      terr.id            as ruian_code,
      terr.level         as level,
      terr.name          as terr_name,
      v.kraj             as kraj,
      v.source_revision  as source_revision,
      case when c.is_nov then v.ref_rent_novostavba_per_m2
           else v.ref_rent_per_m2 end as base
    from cand c
    join lateral (
      select b.id, b.level, b.name
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
      ), 0) as adj_sum,
      coalesce(jsonb_agg(
        jsonb_build_object('attribute', a.attribute, 'czk_per_m2', a.czk_per_m2)
        order by a.attribute
      ) filter (where
           (a.attribute = 'balcony'   and m.has_balcony)
        or (a.attribute = 'terrace'   and m.terrace)
        or (a.attribute = 'furnished' and m.furnished = 'ano')
        or (a.attribute = 'garage'    and m.garage)
        or (a.attribute = 'elevator'  and m.has_lift)
        or (a.attribute = 'other_material' and m.is_nov
            and m.building_type is not null
            and m.building_type not in ('panel', 'cihla'))
      ), '[]'::jsonb) as adj_items
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
            / m.price_czk * 100, 2) as yield_pct,
      jsonb_build_object(
        'territory', jsonb_build_object(
          'ruian_code', m.ruian_code, 'level', m.level,
          'name', m.terr_name, 'kraj', m.kraj),
        'vk', m.vk,
        'is_novostavba', m.is_nov,
        'source_revision', m.source_revision,
        'base_per_m2', m.base,
        'adjustments', coalesce(a.adj_items, '[]'::jsonb),
        'adjustments_sum_per_m2', coalesce(a.adj_sum, 0),
        'total_per_m2', m.base + coalesce(a.adj_sum, 0),
        'area_m2', m.area_m2,
        'monthly_rent_czk',
          round((m.base + coalesce(a.adj_sum, 0)) * m.area_m2)::integer
      ) as detail
    from matched m
    left join adj a on a.sreality_id = m.sreality_id
    where m.base is not null
  ),
  final as (
    select c.sreality_id, comp.rent_czk, comp.yield_pct, comp.detail
    from cand c
    left join computed comp on comp.sreality_id = c.sreality_id
  )
  update listings l
    set mf_reference_rent_czk = f.rent_czk,
        mf_gross_yield_pct    = f.yield_pct,
        mf_reference_rent     = f.detail
  from final f
  where l.sreality_id = f.sreality_id
    and (l.mf_reference_rent_czk is distinct from f.rent_czk
         or l.mf_gross_yield_pct is distinct from f.yield_pct
         or l.mf_reference_rent is distinct from f.detail);

  get diagnostics n = row_count;

  -- Mirror onto the property's representative listing (migration 198) so
  -- properties_public can read p.mf_* without the listings join.
  update properties p
    set mf_reference_rent_czk = l.mf_reference_rent_czk,
        mf_gross_yield_pct    = l.mf_gross_yield_pct
  from listings l
  where l.sreality_id = p.repr_listing_id
    and (p.mf_reference_rent_czk is distinct from l.mf_reference_rent_czk
         or p.mf_gross_yield_pct is distinct from l.mf_gross_yield_pct);

  return n;
end;
$function$;

-- --- 1c. properties_public: read p.mf_* (was l.mf_*) ------------------------
-- Reproduced verbatim from the live definition; only the two mf source
-- columns change from l.* to p.*. Same names/types/positions, so this is a
-- CREATE OR REPLACE (no consumer change).
create or replace view properties_public as
 SELECT p.id AS property_id,
    p.repr_listing_id AS sreality_id,
    p.first_seen_at,
    p.last_seen_at,
    p.is_active,
    p.category_main,
    p.category_type,
    p.current_price_czk AS price_czk,
    l.price_unit,
    p.area_m2,
    p.disposition,
    p.locality,
    p.district,
    l.locality_district_id,
    l.locality_region_id,
    st_y(p.geom::geometry) AS lat,
    st_x(p.geom::geometry) AS lng,
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
        CASE
            WHEN p.is_active THEN GREATEST(0, floor(EXTRACT(epoch FROM now() - p.first_seen_at) / 86400::numeric)::integer)
            ELSE GREATEST(0, floor(EXTRACT(epoch FROM p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
        END AS tom_days,
        CASE
            WHEN p.area_m2 IS NOT NULL AND p.area_m2 > 0::numeric AND p.current_price_czk IS NOT NULL THEN p.current_price_czk::numeric / p.area_m2
            ELSE NULL::numeric
        END AS price_per_m2,
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
    COALESCE(p.street, l.street) AS street,
    p.mf_reference_rent_czk,
    p.mf_gross_yield_pct,
    l.obec,
    l.okres,
    l.region,
    p.home_obec_pop,
    p.near_pop_5km,
    p.near_pop_15km,
    p.near_jobs_5km,
    p.near_jobs_15km,
    p.near_youth_5km,
    p.near_youth_15km,
    p.near_overall_5km,
    p.near_overall_15km,
    p.subtype,
    p.last_change_at,
    l.obec_id,
    l.okres_id,
    l.region_id,
    p.price_change_count,
    p.price_change_count_30d,
    p.price_change_count_90d,
    p.price_change_count_365d,
    p.total_price_change_pct,
    concat_ws(', '::text, COALESCE(p.street, l.street), p.locality) AS place_search_text
   FROM properties p
     LEFT JOIN listings l ON l.sreality_id = p.repr_listing_id
  WHERE p.status = 'active'::text;

grant select on properties_public to anon;

-- --- 1d. one-time backfill --------------------------------------------------
-- Populate the new property columns from current listings figures.
select public.recompute_mf_gross_yields();

-- --- 2. composite keyset indexes (col, id) ---------------------------------
-- last_seen_at + first_seen_at are NOT NULL → one (col, id) btree each serves
-- both scroll directions. price/area are mostly non-null. mf yield is 89%
-- NULL, so a PARTIAL index on the non-null head (the meaningful "highest
-- yield first" pages); the null tail pages via the PK.
create index if not exists properties_last_seen_keyset_idx
  on properties (last_seen_at, id);
create index if not exists properties_first_seen_keyset_idx
  on properties (first_seen_at, id);
create index if not exists properties_price_keyset_idx
  on properties (current_price_czk, id);
create index if not exists properties_area_keyset_idx
  on properties (area_m2, id);
create index if not exists properties_mf_yield_keyset_idx
  on properties (mf_gross_yield_pct, id) where mf_gross_yield_pct is not null;
