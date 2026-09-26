-- 567_mf_read_time_views.sql -- the serving views read MF at read time, with the stored KÚ
-- (MF program PR-B step 2 of 2; drafted as PR-D's 566 at fa7ce8e1, #1622).
--
-- ADDITIVE SWAP. `create or replace` of three views with IDENTICAL column names, types and
-- positions (Postgres enforces all three, and `sync_browse_list` inserts POSITIONALLY);
-- the ONLY change in each body is where the MF columns come from:
--
--   browse_projection    561's body. mf_reference_rent_czk / mf_gross_yield_pct from
--                        LEFT JOIN LATERAL mf_reference(<the property's golden facts>,
--                        <its representative's listing_location codes: obec_kod AND
--                        katastr_kod>) instead of the stored properties.mf_*. Both read
--                        models (browse_list */15, properties_map_mv 7,37) are `select *
--                        from` it, so they carry the read-time value from their next
--                        scheduled rebuild -- no forced rebuild here (a tick that finds the
--                        lock held simply skips).
--   properties_public    508's body. The same lateral, plus the detail jsonb
--                        (mf_reference_rent). Its readers -- the listing page, the kanban
--                        (pipeline_board_public), the Watchdog matcher, the lookup and the
--                        Watchdog feed -- see the read-time value at COMMIT.
--   listing_feed_public  535's body. mf_gross_yield_pct is the PROPERTY's number, read
--                        from the Browse read model by property_id (operator ruling Q8 b):
--                        one computed value per property everywhere, and no second per-row
--                        computation on the portal lane (Gate 0: sorting a 20k cohort by a
--                        computed yield would breach the 3 s read budget).
--
-- WHY THIS WAITS FOR THE KÚ (lead sequencing 2026-09-26). The ministry prices 206 towns per
-- katastrální území; a flat there whose row carries no KÚ gets the town's RANGE on detail
-- surfaces and no Browse yield (operator rulings D1 + Q2). Swapping with the KÚ unbound would
-- have put 79 % of eligible sale flats on a range and out of Browse's yield filter and sort,
-- so the swap passes `ll.katastr_kod` (migration 566, filled by resolver v5.4) and refuses to
-- apply until the re-resolve has filled it -- a fact, not a version string.
--
-- WHY A FUNCTION BETWEEN listing_feed_public AND browse_list. browse_list is rebuilt
-- blue-green (`drop table if exists browse_list` + rename, every */15): a view that named
-- it would block that DROP and every rebuild would fail from then on (537 states the same
-- constraint for its three dismissal-aware sources). A LANGUAGE sql body is not
-- dependency-tracked, and a STABLE, INVOKER, SET-free set-returning function is inlined, so
-- `browse_list_mf` costs a join on browse_list_pk, not a call. It returns the one column the
-- view needs, NOT browse_projection's row type, so it pins nothing either.
--
-- ROLLBACK is a forward migration restating 561 §2 / 508 §1e / 535 §2 verbatim (and dropping
-- browse_list_mf); the stored properties.mf_* columns survive until PR-F.
--
-- APPLY via apply_migration.yml after 565 (the measure) and 566 (the column), once the v5.4
-- re-resolve has drained -- the precondition below refuses until then; a refusal is retried
-- later, never overridden. Statement autocommit around ONE explicit transaction for the
-- swap, every statement idempotent (561's recipe), so a retried file resumes.

-- ---------------------------------------------------------------------------
-- 0. Preconditions: the measure and its inputs exist and answer for the revision every
--    reader will see, and the location lane has stored the KÚ the views call it with. The
--    last two are the re-resolve having FINISHED, stated as facts: no full-sweep row still
--    waits for its first attempt, and every Czech ADDRESS-GRAIN row (`is_address_grain`,
--    never the enum's order) carries a KÚ, whatever resolver version wrote it -- an address
--    point lies in exactly one. That is the KÚ arm's predicate and its one excuse (an obec
--    holding a KÚ recorded `degenerate_boundary_geometry` at the current registry version),
--    but NOT the arm: check_location_town_coverage counts only rows at the current
--    RESOLVER_VERSION, while this counts every row, so a row the re-resolve has not reached
--    yet refuses here too. The refusal names the count per resolver_version.
-- ---------------------------------------------------------------------------
do $$
declare
  n_unfilled bigint;
  by_version text;
begin
  if to_regclass('public.rent_map_cells') is null
     or to_regprocedure('public.mf_reference(text, text, text, numeric, bigint, text, boolean, '
                        'boolean, text, boolean, boolean, text, bigint, bigint, '
                        'public.country_status)') is null then
    raise exception '567 refused: apply 565 (rent_map_cells + mf_reference) first';
  end if;
  if exists (select 1 from public.rent_map_revisions)
     and not exists (select 1 from public.rent_map_cells c
                      where c.source_revision = (select max(source_revision)
                                                   from public.rent_map_revisions)) then
    raise exception '567 refused: rent_map_cells is not populated for the latest rent-map '
                    'revision -- refresh it first';
  end if;
  if not exists (select 1 from pg_attribute
                  where attrelid = 'public.listing_location'::regclass
                    and attname = 'katastr_kod' and not attisdropped) then
    raise exception '567 refused: apply 566 (listing_location.katastr_kod) first';
  end if;
  if exists (select 1 from public.dirty_locations
              where reason = 'full_sweep' and attempts = 0) then
    raise exception '567 refused: the v5.4 re-resolve has not drained (full_sweep rows '
                    'still queued) -- retry later';
  end if;
  select coalesce(sum(n), 0), string_agg(format('%s %s', v, n), ', ' order by v)
    into n_unfilled, by_version
    from (select ll.resolver_version as v, count(*) as n
            from public.listing_location ll
            join public.location_granularity_rank gr on gr.granularity = ll.granularity
           where ll.country_status = 'cz'
             and gr.is_address_grain
             and ll.katastr_kod is null
             and ll.obec_kod not in (
                   select o.code
                     from public.registry_load_discrepancies d
                     join public.ruian_admin_units k
                       on k.level = 'katastralni_uzemi' and k.code = d.entity_code
                      and k.valid_to is null
                     join public.ruian_admin_units o on o.id = k.parent_id
                    where d.entity_kind = 'katastralni_uzemi'
                      and d.discrepancy = 'degenerate_boundary_geometry'
                      and d.registry_version_id = (select id from public.registry_versions
                                                    where is_current))
           group by ll.resolver_version) s;
  if n_unfilled > 0 then
    raise exception '567 refused: % Czech address-grain rows have no katastr_kod (by '
                    'resolver_version: %) -- retry after the re-resolve', n_unfilled, by_version;
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- 1. Both rebuild locks FIRST (522/535's preamble): while they are held every pg_cron
--    tick self-skips in milliseconds, which makes the DDL below uncontended. The acquire
--    QUEUES behind an in-flight rebuild rather than aborting.
-- ---------------------------------------------------------------------------
set statement_timeout = '900s';
set lock_timeout = 0;

select pg_advisory_lock(hashtext('rebuild_browse_list'));
select pg_advisory_lock(hashtext('rebuild_properties_map_mv'));

set lock_timeout = '30s';

begin;

create or replace function public.browse_list_mf(p_property_id bigint)
returns table (mf_gross_yield_pct numeric)
language sql
stable
parallel safe
as $$
  select b.mf_gross_yield_pct from public.browse_list b where b.property_id = p_property_id
$$;

revoke execute on function public.browse_list_mf(bigint) from public, anon;
grant execute on function public.browse_list_mf(bigint) to authenticated, service_role;

-- 561 §2's body; MF re-sourced, nothing else changed.
create or replace view browse_projection as
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
    mf.mf_reference_rent_czk,
    mf.mf_gross_yield_pct,
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
     left join lateral public.mf_reference(
         p.category_main, p.category_type, p.disposition, p.area_m2, p.current_price_czk,
         p.condition, p.has_balcony, p.terrace, p.furnished, p.garage, p.has_lift,
         p.building_type, ll.obec_kod, ll.katastr_kod, ll.country_status) mf on true
where p.status = 'active'::text
  -- THE CONSUMER RULE (rule 25), read off the label join (522).
  and (ll.geom IS NOT NULL OR ll.country_status = 'foreign');

revoke all on browse_projection from anon, authenticated;
grant select on browse_projection to authenticated;

-- 508 §1e's body; MF re-sourced, nothing else changed.
create or replace view properties_public as
select
    p.id as property_id,
    p.repr_listing_id as sreality_id,
    p.first_seen_at,
    p.last_seen_at,
    p.is_active,
    p.category_main,
    p.category_type,
    p.current_price_czk as price_czk,
    l.price_unit,
    p.area_m2,
    p.disposition,
    st_y(ll.geom) as lat,
    st_x(ll.geom) as lng,
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
    null::text as broker_email,
    null::text as broker_phone,
    case
        when p.is_active then greatest(0, floor(extract(epoch from now() - p.first_seen_at) / 86400::numeric)::integer)
        else greatest(0, floor(extract(epoch from p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
    end as tom_days,
    measure_price_per_m2(p.current_price_czk::numeric, p.area_m2, p.category_main, p.category_type) as price_per_m2,
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
    mf.mf_reference_rent_czk,
    mf.mf_gross_yield_pct,
    ll.obec_name as obec,
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
    mf.mf_reference_rent,
    p.published_at,
    p.repr_listing_ref_id as listing_id,
    l.source_id_native,
    measure_price_per_m2_basis(p.category_main, p.category_type) as price_per_m2_basis,
    location_display_label(ll.street_name, ll.house_number_cp, ll.house_number_co,
                           ll.obec_name, ll.cast_obce_name, ll.country_code,
                           ll.country_status) as display_label,
    ll.cast_obce_kod as cast_obce_id
from properties p
     left join listings l on l.id = p.repr_listing_ref_id
     left join listing_location ll on ll.listing_id = p.repr_listing_ref_id
     left join lateral public.mf_reference(
         p.category_main, p.category_type, p.disposition, p.area_m2, p.current_price_czk,
         p.condition, p.has_balcony, p.terrace, p.furnished, p.garage, p.has_lift,
         p.building_type, ll.obec_kod, ll.katastr_kod, ll.country_status) mf on true
where p.status = 'active'::text;

revoke all on properties_public from anon, authenticated;
grant select on properties_public to authenticated;

-- 535 §2's body; mf_gross_yield_pct re-sourced, nothing else changed.
create or replace view listing_feed_public as
select
  l.id,
  l.source,
  l.source_id_native,
  l.sreality_id,
  l.category_main,
  l.category_type,
  l.category_sub_cb,
  l.subtype,
  l.disposition,
  l.price_czk,
  l.price_unit,
  l.area_m2,
  ll.obec_kod  as obec_id,
  ll.okres_kod as okres_id,
  ll.kraj_kod  as region_id,
  st_y(ll.geom) as lat,
  st_x(ll.geom) as lng,
  l.floor,
  l.total_floors,
  l.has_balcony,
  l.has_parking,
  l.has_lift,
  l.building_type,
  l.condition,
  l.energy_rating,
  l.estate_area,
  l.usable_area,
  l.garden_area,
  l.furnished,
  l.terrace,
  l.cellar,
  l.garage,
  l.parking_lots,
  l.ownership,
  l.building_condition_level,
  l.apartment_condition_level,
  l.description,
  l.is_active,
  l.first_seen_at,
  l.last_seen_at,
  l.published_at,
  l.discovery_seq,
  case when l.source in ('bazos', 'ceskereality') then l.published_at end as portal_date,
  measure_price_per_m2(l.price_czk::numeric, l.area_m2::numeric, l.category_main, l.category_type) as price_per_m2,
  l.id as listing_id,
  l.property_id,
  (
    lpad(
      greatest(0, floor(extract(epoch from
        (case when l.source in ('bazos', 'ceskereality') then l.published_at end)
          at time zone 'UTC'
      )))::bigint::text,
      12, '0'
    )
    || lpad(coalesce(l.discovery_seq, 0)::text, 19, '0')
  ) collate "C" as portal_sort_key,
  case when l.is_active
       then greatest(0, floor(extract(epoch from now() - l.first_seen_at) / 86400::numeric)::integer)
       else greatest(0, floor(extract(epoch from l.last_seen_at - l.first_seen_at) / 86400::numeric)::integer)
  end as tom_days,
  rm.mf_gross_yield_pct,
  p.last_change_at,
  p.home_obec_pop,
  p.near_pop_5km, p.near_pop_15km,
  p.near_jobs_5km, p.near_jobs_15km,
  p.near_youth_5km, p.near_youth_15km,
  p.near_overall_5km, p.near_overall_15km,
  p.price_change_count,
  p.price_change_count_30d,
  p.price_change_count_90d,
  p.price_change_count_365d,
  p.total_price_change_pct,
  measure_price_per_m2_basis(l.category_main, l.category_type) as price_per_m2_basis,
  location_display_label(ll.street_name, ll.house_number_cp, ll.house_number_co,
                         ll.obec_name, ll.cast_obce_name, ll.country_code,
                         ll.country_status) as display_label,
  ll.cast_obce_kod as cast_obce_id,
  ll.uncertainty_radius_m,
  gr.rank::int as granularity_rank,
  public.plot_area_m2(l.category_main, l.area_m2::numeric,
                      l.estate_area::numeric) as plot_area_m2
from listings l
join properties p on p.id = l.property_id
left join listing_location ll on ll.listing_id = l.id
left join location_granularity_rank gr on gr.granularity = ll.granularity
left join lateral public.browse_list_mf(l.property_id) rm on true
where p.status = 'active'
  -- THE CONSUMER RULE == location_data.claims_common.SERVED_LOCATION_PREDICATE,
  --    rendered on the FEED's own listing. Pinned by tests/test_location_w5_serve_resolved.py.
  and EXISTS (SELECT 1 FROM listing_location sl WHERE sl.listing_id = l.id AND (sl.geom IS NOT NULL OR sl.country_status = 'foreign'));

revoke all on listing_feed_public from anon, authenticated;
grant select on listing_feed_public to authenticated;

-- ---------------------------------------------------------------------------
-- 2. The proof. A re-source is invisible to `\d` -- the column keeps its name, type and
--    position -- so the only evidence it landed is each view's own definition. Checked
--    both ways: the new source (with the stored KÚ) is present AND the stored columns are
--    no longer read.
-- ---------------------------------------------------------------------------
do $$
declare
  bp text := pg_get_viewdef('public.browse_projection'::regclass);
  pp text := pg_get_viewdef('public.properties_public'::regclass);
  lf text := pg_get_viewdef('public.listing_feed_public'::regclass);
  missing text[] := '{}';
begin
  if position('mf_reference(' in bp) = 0 or position('p.mf_' in bp) > 0
     or position('ll.katastr_kod' in bp) = 0 then
    missing := missing || 'browse_projection does not read MF from mf_reference(.., katastr_kod)';
  end if;
  if position('mf_reference(' in pp) = 0 or position('p.mf_' in pp) > 0
     or position('ll.katastr_kod' in pp) = 0 then
    missing := missing || 'properties_public does not read MF from mf_reference(.., katastr_kod)';
  end if;
  if position('browse_list_mf(' in lf) = 0 or position('l.mf_' in lf) > 0 then
    missing := missing || 'listing_feed_public does not read MF from the read model';
  end if;
  if array_length(missing, 1) is not null then
    raise exception '567 did not land: %', array_to_string(missing, '; ');
  end if;
end $$;

commit;

select pg_advisory_unlock(hashtext('rebuild_browse_list'));
select pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));

select pg_notify('pgrst', 'reload schema');

reset statement_timeout;
reset lock_timeout;
