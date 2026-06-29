-- 257_property_golden_record_mf.sql
--
-- Make the MF reference rent/yield a PROPERTY-grain figure computed from a
-- field-level GOLDEN RECORD, instead of mirroring whichever single representative
-- child happened to win the recompute_property_stats repr lottery.
--
-- WHY
-- The same real-world flat seen on several portals could show two different MF
-- yields. The MF formula is deterministic; the divergence came entirely from
-- (a) per-portal input gaps — a portal that doesn't parse a lift leaves has_lift
-- NULL, which the MF calc reads as "no lift" (no +elevator adjustment) — and
-- (b) NO field-level survivorship on `properties`: recompute_property_stats copied
-- ONE arbitrary child (active, most-recently-seen) for every attribute (only
-- `street` had a best-of rule, migration 183), and recompute_mf_gross_yields then
-- propagated THAT child's per-listing mf_* to properties.mf_*. Measured on prod:
-- 56% of merged sale-apartment properties had an internally-inconsistent child MF
-- reference rent; in ~1,100 the repr child under-stated a recoverable amenity.
--
-- THE FIX (two halves; this migration is the DB half)
--  1. recompute_property_stats.py (code, same PR) now builds `properties.*` with
--     field-level survivorship: amenity booleans OR-union across children (presence
--     wins — validated: of 4,776 lift disagreements only 85 are true-vs-false, the
--     rest NULL-vs-known), area/condition/building_type/etc by source-trust + best
--     non-null, and geom + territory (incl. the new ku_id) from the best CZ-territory
--     child. It also fills the new properties.ku_id.
--  2. recompute_property_mf(p_ids) (NEW here) computes the MF reference rent + yield
--     + breakdown ONCE at property grain from those golden columns — the SAME calc
--     as the per-listing block, just reading properties instead of listings. It is
--     the single authority for properties.mf_*; recompute_mf_gross_yields() now
--     CALLS it (replacing the repr-mirror UPDATE) so a rent-map ingest / hourly run
--     refreshes it, and merge/unmerge call recompute_property_mf(array[id]) inline
--     so a merge's survivor is never one cycle stale.
--
-- The per-listing listings.mf_* is unchanged (still computed per advert — it still
-- feeds listings_public / the listing-detail + extension reads until those surfaces
-- move to property grain in a follow-up). Additive: two nullable columns, one new
-- function, a CREATE OR REPLACE of recompute_mf_gross_yields + properties_public
-- (verbatim + the new breakdown column appended). No destructive change.

alter table properties
  add column if not exists ku_id              bigint,
  add column if not exists mf_reference_rent  jsonb;

comment on column properties.ku_id is
  'Cadastral area (katastrální území) RÚIAN code = admin_boundaries.id (level=ku), '
  'survivorship-picked from the best CZ-territory child by recompute_property_stats. '
  'Internal MF rent-map join key (ku-preferred, obec fallback); not exposed publicly.';
comment on column properties.mf_reference_rent is
  'Property-grain MF Cenová mapa reference-rent breakdown (same shape as '
  'listings.mf_reference_rent) computed from the golden record by '
  'recompute_property_mf(). Always consistent with properties.mf_reference_rent_czk '
  '/ mf_gross_yield_pct. NULL when not computable.';


-- Property-grain MF reference rent / yield / breakdown from the GOLDEN record.
-- p_ids NULL -> every active sale property; else only the given ids (the inline
-- merge/unmerge refresh). Mirrors the per-listing matched/adj/computed exactly,
-- reading the survivorship'd properties columns. Resets ineligible/uncomputable
-- properties to NULL via the cand LEFT JOIN computed (so a property that loses
-- eligibility — bad price, lost territory — doesn't keep a stale value).
create or replace function public.recompute_property_mf(p_ids bigint[] default null)
returns integer
language plpgsql
as $function$
declare
  n integer;
begin
  with cand as (
    select
      p.id, p.category_main, p.ku_id, p.obec_id,
      p.current_price_czk as price_czk, p.area_m2,
      p.has_balcony, p.terrace, p.furnished, p.garage, p.has_lift,
      p.building_type,
      (p.condition = 'novostavba') as is_nov,
      case
        when p.disposition ~ '^[[:space:]]*[01]' then 1
        when p.disposition ~ '^[[:space:]]*2'    then 2
        when p.disposition ~ '^[[:space:]]*3'    then 3
        when p.disposition ~ '^[[:space:]]*[4-9]' then 4
        else null
      end as vk
    from properties p
    where p.category_type = 'prodej'
      and p.status = 'active'
      and (p_ids is null or p.id = any(p_ids))
  ),
  matched as (
    select
      c.id, c.price_czk, c.area_m2, c.vk, c.is_nov,
      c.has_balcony, c.terrace, c.furnished, c.garage, c.has_lift,
      c.building_type,
      coalesce(vku.ruian_code, vob.ruian_code)           as ruian_code,
      coalesce(vku.level, vob.level)                      as level,
      case when vku.ruian_code is not null
           then vku.ku_name else vob.obec_name end        as terr_name,
      coalesce(vku.kraj, vob.kraj)                        as kraj,
      coalesce(vku.source_revision, vob.source_revision)  as source_revision,
      case
        when vku.ruian_code is not null
          then case when c.is_nov then vku.ref_rent_novostavba_per_m2
                    else vku.ref_rent_per_m2 end
        else case when c.is_nov then vob.ref_rent_novostavba_per_m2
                  else vob.ref_rent_per_m2 end
      end                                                 as base
    from cand c
    left join rent_map_values_public vku
      on vku.vk = c.vk and vku.ruian_code = c.ku_id
    left join rent_map_values_public vob
      on vob.vk = c.vk and vob.ruian_code = c.obec_id
    where c.category_main = 'byt'
      and c.vk is not null
      and c.price_czk >= 100000
      and c.area_m2 is not null and c.area_m2 >= 12
      and (vku.ruian_code is not null or vob.ruian_code is not null)
  ),
  adj as (
    select
      m.id,
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
    group by m.id
  ),
  computed as (
    select
      m.id,
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
    left join adj a on a.id = m.id
    where m.base is not null
  ),
  final as (
    select c.id, comp.rent_czk, comp.yield_pct, comp.detail
    from cand c
    left join computed comp on comp.id = c.id
  )
  update properties p
    set mf_reference_rent_czk = f.rent_czk,
        mf_gross_yield_pct    = f.yield_pct,
        mf_reference_rent     = f.detail
  from final f
  where p.id = f.id
    and (p.mf_reference_rent_czk is distinct from f.rent_czk
         or p.mf_gross_yield_pct is distinct from f.yield_pct
         or p.mf_reference_rent is distinct from f.detail);

  get diagnostics n = row_count;
  return n;
end;
$function$;


-- recompute_mf_gross_yields(): the per-listing block is verbatim; the trailing
-- repr-mirror UPDATE is replaced by a call to recompute_property_mf(null) so
-- properties.mf_* is computed from the golden record, not copied from the repr
-- child's per-listing value.
create or replace function public.recompute_mf_gross_yields()
returns integer
language plpgsql
as $function$
declare
  n integer;
begin
  with cand as (
    select
      l.sreality_id, l.category_main, l.ku_id, l.obec_id, l.price_czk, l.area_m2,
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
      coalesce(vku.ruian_code, vob.ruian_code)           as ruian_code,
      coalesce(vku.level, vob.level)                      as level,
      case when vku.ruian_code is not null
           then vku.ku_name else vob.obec_name end        as terr_name,
      coalesce(vku.kraj, vob.kraj)                        as kraj,
      coalesce(vku.source_revision, vob.source_revision)  as source_revision,
      case
        when vku.ruian_code is not null
          then case when c.is_nov then vku.ref_rent_novostavba_per_m2
                    else vku.ref_rent_per_m2 end
        else case when c.is_nov then vob.ref_rent_novostavba_per_m2
                  else vob.ref_rent_per_m2 end
      end                                                 as base
    from cand c
    left join rent_map_values_public vku
      on vku.vk = c.vk and vku.ruian_code = c.ku_id
    left join rent_map_values_public vob
      on vob.vk = c.vk and vob.ruian_code = c.obec_id
    where c.category_main = 'byt'
      and c.vk is not null
      and c.price_czk >= 100000
      and c.area_m2 is not null and c.area_m2 >= 12
      and (vku.ruian_code is not null or vob.ruian_code is not null)
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

  -- properties.mf_* is now computed from the golden record, NOT mirrored from the
  -- representative child's per-listing mf_*.
  perform public.recompute_property_mf(null);

  return n;
end;
$function$;


-- properties_public: reproduced verbatim from the live definition with the new
-- p.mf_reference_rent breakdown appended (trailing) so the listing-detail header
-- can read a property-grain breakdown in the follow-up read-surface PR.
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
    p.locality_district_id,
    p.locality_region_id,
    p.lat,
    p.lng,
    l.floor,
    l.total_floors,
    p.has_balcony,
    p.has_parking,
    p.has_lift,
    p.building_type,
    p.condition,
    p.energy_rating,
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
            WHEN p.area_m2 IS NOT NULL AND p.area_m2 > 0::numeric AND p.current_price_czk IS NOT NULL THEN round(p.current_price_czk::numeric / p.area_m2, 2)
            ELSE NULL::numeric
        END AS price_per_m2,
    p.building_condition_level,
    p.apartment_condition_level,
    l.description,
    p.source_count,
    p.distinct_site_count,
    p.price_drop_count,
    p.price_rise_count,
    p.max_price_drop_pct,
    p.stats_computed_at,
    p.source,
    COALESCE(p.street, l.street) AS street,
    p.mf_reference_rent_czk,
    p.mf_gross_yield_pct,
    p.obec,
    p.okres,
    p.region,
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
    p.obec_id,
    p.okres_id,
    p.region_id,
    p.price_change_count,
    p.price_change_count_30d,
    p.price_change_count_90d,
    p.price_change_count_365d,
    p.total_price_change_pct,
    concat_ws(', '::text, p.street, p.locality) AS place_search_text,
    p.asset_id,
    p.mf_reference_rent
   FROM properties p
     LEFT JOIN listings l ON l.sreality_id = p.repr_listing_id
  WHERE p.status = 'active'::text;

grant select on properties_public to anon;
