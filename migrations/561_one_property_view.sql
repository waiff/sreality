-- 561_one_property_view.sql -- AUTODEDUP W4, one property record, one property view
-- (decisions 12 and 18).
--
-- 1. `property_canonical_listings(property_id)`: THE ONE ORDER a property's adverts speak in --
--    active first, then `source_trust_rank` (311), then the most recently seen, then the lowest
--    id (1,840 properties were tied at the top and could flip between runs). Rank 1 is the
--    CANONICAL advert. Its caller is the rollup (scripts/recompute_property_stats.py): every
--    advert field is the canonical advert's, every physical fact the first non-empty value in
--    this same order. Language sql, stable, invoker, no SET: the planner inlines it.
-- 2. `all_sources` / `active_sources` leave the Browse read model. Nothing ever wrote them (425
--    projected two columns that exist only on production). Removing a view column is DROP +
--    CREATE, which takes the view's row-type dependants with it (537's two dismissal-aware
--    sources and `properties_map_mv`), so all three come back here; `browse_list`, the
--    disposable cache every */15 rebuild re-creates, loses the two all-NULL columns in the SAME
--    transaction, so `sync_browse_list`'s positional insert never sees two shapes. The PHYSICAL
--    `properties` columns stay: that drop is destructive (rule 1) and W8's, with its backup.
--
-- APPLY BEFORE THE CODE MERGES (the rollup calls the function with no fallback), OFF-HOURS: the
-- map is absent for its ~5-minute rebuild, the window 508 took. Statement autocommit around one
-- explicit transaction for the swap, every statement idempotent (like 522/535), so a retried
-- file resumes. Verify: `select * from property_canonical_listings(<id>)`, and `all_sources` is in
-- no pg_attribute row of browse_projection / browse_list / properties_map_mv.

set statement_timeout = '900s';
set lock_timeout = 0;

select pg_advisory_lock(hashtext('rebuild_browse_list'));
select pg_advisory_lock(hashtext('rebuild_properties_map_mv'));

set lock_timeout = '30s';

-- 1. The one canonical order.
create or replace function public.property_canonical_listings(p_property_id bigint)
returns table (listing_id bigint, canonical_rank integer)
language sql
stable
as $$
  select l.id,
         (row_number() over (
            order by l.is_active desc, public.source_trust_rank(l.source),
                     l.last_seen_at desc nulls last, l.id))::integer
    from public.listings l
   where l.property_id = p_property_id
$$;

revoke execute on function public.property_canonical_listings(bigint) from public, anon, authenticated;
grant execute on function public.property_canonical_listings(bigint) to service_role;

-- 2. 535's projection less the two columns, every other expression and its order unchanged
--    (each re-sourced column is explained where 503/514/522/535 introduced it).
begin;

drop function if exists public.browse_list_visible();
drop function if exists public.properties_map_visible();
drop materialized view if exists public.properties_map_mv;
drop view if exists public.browse_projection;

create view browse_projection as
select
    p.id as property_id,
    p.repr_listing_id as sreality_id,
    p.first_seen_at,
    p.last_seen_at,
    p.is_active,
    p.category_main,
    p.category_type,
    p.current_price_czk as price_czk,
    p.area_m2,
    p.disposition,
    st_y(ll.geom) as lat,
    st_x(ll.geom) as lng,
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
    case
        when p.is_active then greatest(0, floor(extract(epoch from now() - p.first_seen_at) / 86400::numeric)::integer)
        else greatest(0, floor(extract(epoch from p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
    end as tom_days,
    measure_price_per_m2(p.current_price_czk::numeric, p.area_m2, p.category_main, p.category_type) as price_per_m2,
    p.building_condition_level,
    p.apartment_condition_level,
    p.source,
    p.mf_reference_rent_czk,
    p.mf_gross_yield_pct,
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
    ll.obec_kod  as obec_id,
    ll.okres_kod as okres_id,
    ll.kraj_kod  as region_id,
    p.price_change_count,
    p.price_change_count_30d,
    p.price_change_count_90d,
    p.price_change_count_365d,
    p.total_price_change_pct,
    p.asset_id,
    p.repr_listing_ref_id as listing_id,
    (select l.source_id_native from listings l where l.id = p.repr_listing_ref_id) as source_id_native,
    measure_price_per_m2_basis(p.category_main, p.category_type) as price_per_m2_basis,
    location_display_label(ll.street_name, ll.house_number_cp, ll.house_number_co,
                           ll.obec_name, ll.cast_obce_name, ll.country_code,
                           ll.country_status) as display_label,
    ll.cast_obce_kod as cast_obce_id,
    ll.uncertainty_radius_m,
    gr.rank::int as granularity_rank,
    public.plot_area_m2(p.category_main, p.area_m2::numeric,
                        p.estate_area::numeric) as plot_area_m2
from properties p
     left join listing_location ll on ll.listing_id = p.repr_listing_ref_id
     left join location_granularity_rank gr on gr.granularity = ll.granularity
where p.status = 'active'::text
  -- THE CONSUMER RULE (rule 25), read off the label join (522).
  and (ll.geom IS NOT NULL OR ll.country_status = 'foreign');

revoke all on browse_projection from anon, authenticated;
grant select on browse_projection to authenticated;

alter table public.browse_list
  drop column if exists all_sources,
  drop column if exists active_sources;

-- 537's body, verbatim.
create or replace function public.browse_list_visible()
returns setof public.browse_projection
language sql
stable
as $$
  select l.* from public.browse_list l
  where not exists (
    select 1 from public.property_dismissals_public d where d.property_id = l.property_id)
$$;

revoke execute on function public.browse_list_visible() from public, anon;
grant execute on function public.browse_list_visible() to authenticated, service_role;

commit;

-- 3. The map, rebuilt by the function pg_cron runs (this session holds its re-entrant advisory
--    key, so it runs rather than skips), then its dismissal-aware source (537's body) over it.
set lock_timeout = 0;

select public.rebuild_properties_map_mv();

create or replace function public.properties_map_visible()
returns setof public.browse_projection
language sql
stable
as $$
  select m.* from public.properties_map_mv m
  where not exists (
    select 1 from public.property_dismissals_public d where d.property_id = m.property_id)
$$;

revoke execute on function public.properties_map_visible() from public, anon;
grant execute on function public.properties_map_visible() to authenticated, service_role;

select pg_advisory_unlock(hashtext('rebuild_browse_list'));
select pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));

select pg_notify('pgrst', 'reload schema');

reset statement_timeout;
reset lock_timeout;
