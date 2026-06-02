-- 142_property_city_proximity.sql
--
-- Precomputed city-proximity columns on `properties`, so Browse / Watchdog can
-- answer "within 5 / 15 km of a municipality with population > N" and "within
-- 5 / 15 km of a curated city whose jobs / youth-migration / overall index > T"
-- as plain indexed-column predicates — no per-request spatial RPC, no
-- prefilter→.in(ids) round trip, no anon 3s statement-timeout.
--
-- Design (mirrors recompute_mf_gross_yields, migration 133):
--   * Radii are FIXED at 5 km and 15 km (two columns per metric). The
--     THRESHOLD stays dynamic: each column stores the MAX value found within
--     that radius, so the filter is `column >= user_threshold` for any
--     threshold the operator types.
--   * Distance is POLYGON-EDGE (ST_DWithin on the municipality MultiPolygon —
--     0 m when the point is inside), which is what "within X km of a city"
--     means on the ground: a flat near Brno's boundary is "within 5 km of
--     Brno" even though Brno's centroid is 12 km away.
--   * home_obec_pop is the population of the listing's OWN municipality
--     (nearest obec polygon = the containing one), driving the Min Population
--     filter for every listing — not just the 206 curated cities.
--
-- Population proximity considers obce with population >= 10 000 (the smallest
-- threshold the Browse UI offers); index proximity considers the 206 curated
-- cities (only they carry the qualitative indexes). Both sets are tiny (~215
-- rows together), so recompute_city_proximity() spatial-joins each property
-- against a small GiST-indexed anchor set — ~2 ms/property, a few minutes for
-- the whole table.
--
-- Values are filled by recompute_city_proximity() (incremental by default;
-- pass true for a full rebuild), run by recompute_city_proximity.yml and after
-- a population / city-index data load.

alter table properties
  add column if not exists home_obec_pop              integer,
  add column if not exists near_pop_5km               integer,
  add column if not exists near_pop_15km              integer,
  add column if not exists near_jobs_5km              numeric,
  add column if not exists near_jobs_15km             numeric,
  add column if not exists near_youth_5km             numeric,
  add column if not exists near_youth_15km            numeric,
  add column if not exists near_overall_5km           numeric,
  add column if not exists near_overall_15km          numeric,
  add column if not exists city_proximity_computed_at timestamptz;

comment on column properties.home_obec_pop is
  'Population of the listing''s own municipality (nearest obec polygon). Min Population filter.';
comment on column properties.near_pop_5km is
  'Max population among obce (pop>=10000) whose polygon is within 5 km. Filter: >= threshold.';

create index if not exists properties_home_obec_pop_idx     on properties (home_obec_pop)     where home_obec_pop is not null;
create index if not exists properties_near_pop_5km_idx      on properties (near_pop_5km)      where near_pop_5km is not null;
create index if not exists properties_near_pop_15km_idx     on properties (near_pop_15km)     where near_pop_15km is not null;
create index if not exists properties_near_jobs_5km_idx     on properties (near_jobs_5km)     where near_jobs_5km is not null;
create index if not exists properties_near_jobs_15km_idx    on properties (near_jobs_15km)    where near_jobs_15km is not null;
create index if not exists properties_near_youth_5km_idx    on properties (near_youth_5km)    where near_youth_5km is not null;
create index if not exists properties_near_youth_15km_idx   on properties (near_youth_15km)   where near_youth_15km is not null;
create index if not exists properties_near_overall_5km_idx  on properties (near_overall_5km)  where near_overall_5km is not null;
create index if not exists properties_near_overall_15km_idx on properties (near_overall_15km) where near_overall_15km is not null;

create or replace function recompute_city_proximity(p_full boolean default false)
returns integer
language plpgsql
as $$
declare
  n integer;
begin
  -- Anchor set: obce big enough to satisfy the smallest pop threshold the UI
  -- offers (>=10000), plus the 206 curated cities that carry the qualitative
  -- indexes (latest revision). One GiST-indexed temp set keeps the per-property
  -- spatial probe over a few hundred rows instead of all ~6 250 obce.
  drop table if exists _prox_anchors;
  create temp table _prox_anchors on commit drop as
  with latest as (select max(source_revision) as rev from city_index_values),
  idx as (
    select cc.admin_boundary_id as ab_id,
      max(civ.value) filter (where civ.index_name = 'pracovni_mista')    as jobs,
      max(civ.value) filter (where civ.index_name = 'stehovani_mladych') as youth,
      max(civ.value) filter (where civ.index_name = 'celkove_hodnoceni') as overall
    from curated_cities cc
    join city_index_values civ
      on civ.city_id = cc.id
     and civ.source_revision = (select rev from latest)
     and civ.index_name in ('pracovni_mista', 'stehovani_mladych', 'celkove_hodnoceni')
    where cc.admin_boundary_id is not null
    group by cc.admin_boundary_id
  )
  select ab.id as ab_id, ab.geom,
    case when ab.population >= 10000 then ab.population end as population,
    idx.jobs, idx.youth, idx.overall
  from admin_boundaries ab
  left join idx on idx.ab_id = ab.id
  where ab.level = 'obec' and ab.geom is not null
    and (ab.population >= 10000 or idx.ab_id is not null);

  create index on _prox_anchors using gist (geom);
  analyze _prox_anchors;

  update properties p set
    home_obec_pop    = s.home_obec_pop,
    near_pop_5km     = s.near_pop_5km,
    near_pop_15km    = s.near_pop_15km,
    near_jobs_5km    = s.near_jobs_5km,
    near_jobs_15km   = s.near_jobs_15km,
    near_youth_5km   = s.near_youth_5km,
    near_youth_15km  = s.near_youth_15km,
    near_overall_5km = s.near_overall_5km,
    near_overall_15km= s.near_overall_15km,
    city_proximity_computed_at = now()
  from (
    select p2.id,
      hp.population as home_obec_pop,
      a5.pop  as near_pop_5km,  a15.pop  as near_pop_15km,
      a5.jobs as near_jobs_5km, a15.jobs as near_jobs_15km,
      a5.youth as near_youth_5km, a15.youth as near_youth_15km,
      a5.overall as near_overall_5km, a15.overall as near_overall_15km
    from properties p2
    left join lateral (
      select ab.population from admin_boundaries ab
      where ab.level = 'obec'
      order by p2.geom <-> ab.geom
      limit 1
    ) hp on true
    left join lateral (
      select max(population) as pop, max(jobs) as jobs, max(youth) as youth, max(overall) as overall
      from _prox_anchors a where st_dwithin(p2.geom, a.geom, 5000)
    ) a5 on true
    left join lateral (
      select max(population) as pop, max(jobs) as jobs, max(youth) as youth, max(overall) as overall
      from _prox_anchors a where st_dwithin(p2.geom, a.geom, 15000)
    ) a15 on true
    where p2.geom is not null
      and (p_full or p2.city_proximity_computed_at is null)
  ) s
  where p.id = s.id;

  get diagnostics n = row_count;
  return n;
end
$$;

comment on function recompute_city_proximity(boolean) is
  'Fill properties.home_obec_pop + near_{pop,jobs,youth,overall}_{5,15}km. '
  'Incremental (city_proximity_computed_at IS NULL) unless p_full. '
  'Run by recompute_city_proximity.yml and after a population / city-index load.';

-- Expose the nine filterable columns on the anon-facing view (append only).
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
    l.street,
    l.mf_reference_rent_czk,
    l.mf_gross_yield_pct,
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
    p.near_overall_15km
   FROM properties p
     LEFT JOIN listings l ON l.sreality_id = p.repr_listing_id
  WHERE p.status = 'active'::text;
