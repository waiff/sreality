-- 590_read_model_portal_rule.sql -- MERGE SPRINT W5 (docs/design/merge-sprint/PROGRAM.md MS19, MS21,
-- §5's W5 gate, §6): THE one read-model rewrite. Views and functions only; no table data moves.
--
-- 1. browse_projection (-> browse_list, properties_map_mv): `asset_id` leaves (W6 drops
--    properties.asset_id and re-checks that no view names it); the two portal lists (MS21: kept and
--    reused; coalesced, never NULL, because `not.ov` drops a NULL row), `source_count` (the Browse
--    card's "N inzeráty": every ad, active or not) and the nine `newest_ad_at_<portal>` dates
--    (migration 588; the rollup writes them since W2a) are appended LAST, the dates in
--    toolkit.filter_registry.PORTAL_OPTIONS order. rebuild_browse_list() (522's body, read live)
--    gains one partial index per portal from nine static lines: the one-portal "Newest first" /
--    "Oldest first" page (MS19, Q49 b) and the one-portal count.
-- 2. properties_public: `asset_id`, `distinct_site_count` (written by nothing since W2a) and
--    `published_at` (the dedup session's column, dropped by it after W6) leave; the two portal lists
--    are appended (the Watchdog's portal rule). Removing a column is DROP + CREATE, so
--    pipeline_board_public, its one dependant, is re-created byte for byte (508/584 body).
-- 3. public.portal_status_matches(): THE portal rule in SQL (MS19). A property matches portal set P
--    when any of its ads is on P; the status switch is judged on those ads; no portal = the
--    property's own is_active. Read by both aggregate RPCs and the broker lookup
--    (toolkit/brokers.broker_property_ids); the SPA's PostgREST rendering
--    (frontend/src/lib/queries.ts applyPortalRule) and the Watchdog's compiled clause
--    (toolkit/filter_compiler PROPERTIES_GRAIN) are pinned to it by tests/fixtures/portal_rule.json.
-- 4. browse_stats_properties / browse_map_cells: 549's bodies (the live ones, md5-gated) with the
--    two status arms and the canonical-portal arm replaced by ONE call. Parameter lists unchanged
--    (CREATE OR REPLACE keeps the ACL); browse_map_cells keeps `listing_ids_filter` until W6.
--
-- THE ORDER (561's and 584's recipe; 517's rename-aside so the map never goes dark):
--   0. Preconditions, before any lock: every body this file restates or relies on is the one it
--      was written against; one full recompute cycle BEGAN after W2a's deploy and is complete; no
--      daily sweep holds the maintenance lease; a bounded parity sample of the stored lists/dates.
--   1. Both rebuild advisory locks, queued (lock_timeout 0, 1900 s budget); then fail fast (5 s).
--   2. TX1 (one DO block, so ONE transaction, guarded so a re-run skips it): browse_list FIRST
--      (sync_browse_list's lock order) loses asset_id and gains the 12 columns in the projection's
--      order (the positional sync_browse_list insert never sees two shapes); the old projection is
--      RENAMED aside (properties_map_mv and its source follow it by OID and keep serving), the new
--      one created; browse_list_visible() is re-pointed; properties_map_visible() becomes a static
--      BRIDGE that names its columns, valid over the old matview and the new one; properties_public
--      and the board.
--   2b. rebuild_browse_list() replaced after TX1 (the comment there says why).
--   3. The list, rebuilt (forced: this session holds the key): fills the lists, the count and the
--      dates, builds the nine indexes.
--   4. TX2: the map rebuilt the same way, its source restored to 561's body, the legacy view
--      dropped (nothing depends on it once the old matview is gone), and both RPCs switched to the
--      portal rule in the same commit, after the two relations they read carry the columns.
--   5. Post-conditions; 6. unlock, PostgREST reload.
--
-- APPLY OFF-PEAK (02:30-04:00 UTC: list rebuild p50 222-242 s, map ~215 s), through
-- apply_migration.yml, IMMEDIATELY BEFORE merging W5's code: from TX2's commit the map and Stats
-- follow MS19 while the deployed list still filters the canonical ad's portal, until the deploy.
-- Never while the daily recompute runs (step 0 refuses). No ci-allow-dynamic marker: no EXECUTE
-- builds a view, function or table here.
-- ROLLBACK: forward only (rule 1); a re-run resumes (TX1 is guarded, every later step idempotent).

-- ---------------------------------------------------------------------------
-- 0. Preconditions (production only: the CI replay container has < 100k properties).
-- ---------------------------------------------------------------------------
set statement_timeout = '900s';

do $pre$
declare
  populated bool;
  v_stats text;
  v_map text;
  v_cycle timestamptz;
  n_checked bigint;
  n_wrong bigint;
begin
  select count(*) = 100000 into populated
    from (select 1 from public.properties limit 100000) probe;
  if not populated then
    raise notice '590: replay container (corpus < 100k properties), preconditions skipped';
    return;
  end if;

  -- The bodies this file restates or relies on (md5 of prosrc, read live 2026-10-07), or this
  -- file's own (a re-run).
  if (select md5(prosrc) from pg_proc where oid = to_regprocedure('public.rebuild_browse_list()'))
     not in ('1386c849579ec1e490f0ad3447f9c895', '8af37dfbacfecab1a73353bf36d24fba') then
    raise exception '590 refused: rebuild_browse_list() is neither 522''s body nor this file''s';
  end if;
  if (select md5(prosrc) from pg_proc where oid = to_regprocedure('public.rebuild_properties_map_mv()'))
     is distinct from 'e18ea5161c8af9ceab5b07a62b6dc37d' then
    raise exception '590 refused: rebuild_properties_map_mv() is not 522''s body';
  end if;
  if (select md5(prosrc) from pg_proc where oid = to_regprocedure('public.browse_list_visible()'))
     is distinct from '47291211d34d2d14ca403dd3bbcdf176' then
    raise exception '590 refused: browse_list_visible() is not 537/561''s body';
  end if;
  if (select md5(prosrc) from pg_proc where oid = to_regprocedure('public.properties_map_visible()'))
     not in ('87ac5ed4f4c1eaaef58a06c9a4e42071', '37a49e149d762fa038bd9fdd2e74dd07') then
    raise exception '590 refused: properties_map_visible() is neither 561''s body nor this file''s bridge';
  end if;
  select string_agg(md5(p.prosrc), ',') into v_stats from pg_proc p
   where p.pronamespace = 'public'::regnamespace and p.proname = 'browse_stats_properties';
  select string_agg(md5(p.prosrc), ',') into v_map from pg_proc p
   where p.pronamespace = 'public'::regnamespace and p.proname = 'browse_map_cells';
  if v_stats is null or v_stats not in ('cbbc44a4f7543968f2596bcd47a89b95', 'f4d95769ccc20ab9968d0135b86cac26')
     or v_map is null or v_map not in ('5af4e22fa0c2d6e47d841d2502d9852c', '0a1cf3a94fce99941adca7cbe1f375b9') then
    raise exception '590 refused: browse_stats_properties (%) / browse_map_cells (%) are neither 549''s '
                    'bodies nor this file''s: a parallel branch redefined them. Regenerate this file''s two '
                    'RPCs from the live bodies (same two edits) instead of applying it as written.',
                    v_stats, v_map;
  end if;

  -- The data the read model will publish: a full recompute cycle that BEGAN after W2a went live
  -- (Railway, 2026-10-06 19:57 UTC) has completed, and no daily sweep is running.
  select (value->>'cycle_started_at')::timestamptz into v_cycle
    from public.app_settings where key = 'property_sweep_last_complete';
  if v_cycle is null or v_cycle <= timestamptz '2026-10-06 19:57:46+00' then
    raise exception '590 refused: no complete recompute cycle began after W2a (cycle_started_at %)', v_cycle;
  end if;
  if exists (select 1 from public.property_maintenance_lease
              where holder like 'full:%' and expires_at > now()) then
    raise exception '590 refused: the daily recompute holds the maintenance lease';
  end if;

  -- A bounded parity sample: stored lists and dates equal what the ads say (queued properties excepted).
  with props as (
    select p.id, p.all_sources, p.active_sources,
           array[p.newest_ad_at_sreality, p.newest_ad_at_bazos, p.newest_ad_at_idnes,
                 p.newest_ad_at_maxima, p.newest_ad_at_ceskereality, p.newest_ad_at_bezrealitky,
                 p.newest_ad_at_mmreality, p.newest_ad_at_remax, p.newest_ad_at_realitymix] as dates
      from public.properties p
     where p.id >= 600000 and p.id < 602500 and p.status = 'active'
       and not exists (select 1 from public.dirty_properties q where q.property_id = p.id)
  ), truth as (
    select l.property_id as pid,
           array_agg(distinct l.source order by l.source) as all_s,
           coalesce(array_agg(distinct l.source order by l.source) filter (where l.is_active), '{}'::text[]) as active_s,
           array[max(l.first_seen_at) filter (where l.source = 'sreality'),
                 max(l.first_seen_at) filter (where l.source = 'bazos'),
                 max(l.first_seen_at) filter (where l.source = 'idnes'),
                 max(l.first_seen_at) filter (where l.source = 'maxima'),
                 max(l.first_seen_at) filter (where l.source = 'ceskereality'),
                 max(l.first_seen_at) filter (where l.source = 'bezrealitky'),
                 max(l.first_seen_at) filter (where l.source = 'mmreality'),
                 max(l.first_seen_at) filter (where l.source = 'remax'),
                 max(l.first_seen_at) filter (where l.source = 'realitymix')] as dates
      from props p join public.listings l on l.property_id = p.id
     group by l.property_id
  )
  select count(*),
         count(*) filter (where p.all_sources is distinct from t.all_s
                             or p.active_sources is distinct from t.active_s
                             or p.dates is distinct from t.dates)
    into n_checked, n_wrong
    from props p join truth t on t.pid = p.id;
  if n_checked = 0 or n_wrong > 0 then
    raise exception '590 refused: % of % sampled properties store portal lists or dates that differ '
                    'from their ads (the post-W2a cycle has not rewritten them)', n_wrong, n_checked;
  end if;
end
$pre$;

-- ---------------------------------------------------------------------------
-- 1. Both rebuild locks first, queued behind an in-flight tick (a list tick may hold its key up
--    to its own 1800 s cap); from then on every pg_cron tick self-skips. Then fail fast.
-- ---------------------------------------------------------------------------
set statement_timeout = '1900s';
set lock_timeout = 0;

select pg_advisory_lock(hashtext('rebuild_browse_list'));
select pg_advisory_lock(hashtext('rebuild_properties_map_mv'));

set lock_timeout = '5s';
set statement_timeout = '900s';

-- ---------------------------------------------------------------------------
-- The portal rule (MS19). Inlinable: sql, immutable, invoker, no SET; under the RPCs'
-- force_custom_plan the CASE folds to one arm per call.
-- ---------------------------------------------------------------------------
create or replace function public.portal_status_matches(
  p_is_active boolean, p_all_sources text[], p_active_sources text[], p_portals text[], p_status text)
returns boolean
language sql
immutable
parallel safe
as $$
  select case
    when coalesce(cardinality(p_portals), 0) = 0 then
      case p_status when 'active' then p_is_active
                    when 'inactive' then not p_is_active
                    else true end
    when p_status = 'active'   then p_active_sources && p_portals
    when p_status = 'inactive' then p_all_sources && p_portals and not (p_active_sources && p_portals)
    else p_all_sources && p_portals
  end
$$;

revoke execute on function public.portal_status_matches(boolean, text[], text[], text[], text) from public, anon;
grant execute on function public.portal_status_matches(boolean, text[], text[], text[], text) to authenticated, service_role;

comment on function public.portal_status_matches(boolean, text[], text[], text[], text) is
  'MS19 (migration 590): THE portal rule. A property matches portal set P when any of its ads is on '
  'P; status (any / active / inactive) is judged on those ads; with no portal, on the property. '
  'Pinned to the SPA and the Watchdog by tests/fixtures/portal_rule.json.';

-- ---------------------------------------------------------------------------
-- 2. TX1: every shape, one transaction. Guarded: a re-run after TX1 committed skips it.
-- ---------------------------------------------------------------------------
do $tx1$
begin
  if exists (select 1 from pg_attribute
              where attrelid = 'public.browse_projection'::regclass
                and attname = 'newest_ad_at_realitymix' and not attisdropped) then
    raise notice '590: TX1 already applied (browse_projection carries the portal dates), skipped';
    return;
  end if;

  -- The disposable cache FIRST, in sync_browse_list's own lock order (the table, then the view;
  -- 584's reason b), so a patch in flight cannot deadlock this transaction. At commit its columns
  -- equal the new projection's, so that positional insert stays aligned; the 12 are NULL until step 3.
  alter table public.browse_list
    drop column if exists asset_id,
    add column if not exists all_sources text[],
    add column if not exists active_sources text[],
    add column if not exists source_count integer,
    add column if not exists newest_ad_at_sreality timestamptz,
    add column if not exists newest_ad_at_bazos timestamptz,
    add column if not exists newest_ad_at_idnes timestamptz,
    add column if not exists newest_ad_at_maxima timestamptz,
    add column if not exists newest_ad_at_ceskereality timestamptz,
    add column if not exists newest_ad_at_bezrealitky timestamptz,
    add column if not exists newest_ad_at_mmreality timestamptz,
    add column if not exists newest_ad_at_remax timestamptz,
    add column if not exists newest_ad_at_realitymix timestamptz;

  -- The old projection steps aside. properties_map_mv and properties_map_visible() follow it by
  -- OID and keep serving the old shape until step 4 replaces both.
  alter view public.browse_projection rename to browse_projection_legacy;
  revoke all on public.browse_projection_legacy from anon, authenticated;

  -- 584's body (the live one): asset_id out; the two portal lists, the ad count and nine dates
  -- appended LAST, the dates in PORTAL_OPTIONS order (tests/test_recompute_property_stats.py pins
  -- the codes).
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
                          p.estate_area::numeric) as plot_area_m2,
      ll.ulice_kod as ulice_id,
      coalesce(p.all_sources, '{}'::text[]) as all_sources,
      coalesce(p.active_sources, '{}'::text[]) as active_sources,
      p.source_count,
      p.newest_ad_at_sreality,
      p.newest_ad_at_bazos,
      p.newest_ad_at_idnes,
      p.newest_ad_at_maxima,
      p.newest_ad_at_ceskereality,
      p.newest_ad_at_bezrealitky,
      p.newest_ad_at_mmreality,
      p.newest_ad_at_remax,
      p.newest_ad_at_realitymix
  from properties p
       left join listing_location ll on ll.listing_id = p.repr_listing_ref_id
       left join location_granularity_rank gr on gr.granularity = ll.granularity
       left join lateral public.mf_reference(
           p.category_main, p.category_type, p.disposition, p.area_m2, p.current_price_czk,
           p.condition, p.has_balcony, p.terrace, p.furnished, p.garage, p.has_lift,
           p.building_type, ll.obec_kod, ll.katastr_kod, ll.country_status) mf on true
  where p.status = 'active'::text
    -- THE CONSUMER RULE (rule 25), read off the label join (522).
    and (ll.geom IS NOT NULL OR ll.country_status = 'foreign')
  ;
  revoke all on public.browse_projection from anon, authenticated;
  grant select on public.browse_projection to authenticated;

  -- The two dismissal-aware sources return the projection's row type, which just changed:
  -- DROP + CREATE (a return type cannot be replaced), ACL re-stated. 537/561's list body, verbatim.
  drop function public.browse_list_visible();
  create function public.browse_list_visible()
  returns setof public.browse_projection
  language sql
  stable
  as $fn$
  select l.* from public.browse_list l
  where not exists (
    select 1 from public.property_dismissals_public d where d.property_id = l.property_id)
$fn$;
  revoke execute on function public.browse_list_visible() from public, anon;
  grant execute on function public.browse_list_visible() to authenticated, service_role;

  -- The map's source, as a BRIDGE until step 4: it names its columns, so it is valid over the old
  -- matview (which still has asset_id) and over the new one (should a pg_cron tick rebuild it after
  -- a crash between the steps), returning the 12 new columns as NULL meanwhile.
  drop function public.properties_map_visible();
  create function public.properties_map_visible()
  returns setof public.browse_projection
  language sql
  stable
  as $fn$
  select m.property_id, m.sreality_id, m.first_seen_at, m.last_seen_at, m.is_active,
         m.category_main, m.category_type, m.price_czk, m.area_m2, m.disposition, m.lat, m.lng,
         m.has_balcony, m.has_parking, m.has_lift, m.building_type, m.condition, m.energy_rating,
         m.estate_area, m.usable_area, m.garden_area, m.category_sub_cb, m.furnished, m.terrace,
         m.cellar, m.garage, m.parking_lots, m.ownership, m.tom_days, m.price_per_m2,
         m.building_condition_level, m.apartment_condition_level, m.source,
         m.mf_reference_rent_czk, m.mf_gross_yield_pct, m.home_obec_pop,
         m.near_pop_5km, m.near_pop_15km, m.near_jobs_5km, m.near_jobs_15km,
         m.near_youth_5km, m.near_youth_15km, m.near_overall_5km, m.near_overall_15km,
         m.subtype, m.last_change_at, m.obec_id, m.okres_id, m.region_id,
         m.price_change_count, m.price_change_count_30d, m.price_change_count_90d,
         m.price_change_count_365d, m.total_price_change_pct, m.listing_id, m.source_id_native,
         m.price_per_m2_basis, m.display_label, m.cast_obce_id, m.uncertainty_radius_m,
         m.granularity_rank, m.plot_area_m2, m.ulice_id,
         null::text[], null::text[], null::integer,
         null::timestamptz, null::timestamptz, null::timestamptz, null::timestamptz, null::timestamptz,
         null::timestamptz, null::timestamptz, null::timestamptz, null::timestamptz
    from public.properties_map_mv m
   where not exists (
     select 1 from public.property_dismissals_public d where d.property_id = m.property_id)
$fn$;
  revoke execute on function public.properties_map_visible() from public, anon;
  grant execute on function public.properties_map_visible() to authenticated, service_role;

  -- properties_public: 584's body (the live one) less asset_id, distinct_site_count and published_at,
  -- the two portal lists appended. The board reads it, so both are re-created, never with CASCADE.
  drop view public.pipeline_board_public;
  drop view public.properties_public;

  create view properties_public as
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
      mf.mf_reference_rent,
      p.repr_listing_ref_id as listing_id,
      l.source_id_native,
      measure_price_per_m2_basis(p.category_main, p.category_type) as price_per_m2_basis,
      location_display_label(ll.street_name, ll.house_number_cp, ll.house_number_co,
                             ll.obec_name, ll.cast_obce_name, ll.country_code,
                             ll.country_status) as display_label,
      ll.cast_obce_kod as cast_obce_id,
      ll.ulice_kod as ulice_id,
      coalesce(p.all_sources, '{}'::text[]) as all_sources,
      coalesce(p.active_sources, '{}'::text[]) as active_sources
  from properties p
       left join listings l on l.id = p.repr_listing_ref_id
       left join listing_location ll on ll.listing_id = p.repr_listing_ref_id
       left join lateral public.mf_reference(
           p.category_main, p.category_type, p.disposition, p.area_m2, p.current_price_czk,
           p.condition, p.has_balcony, p.terrace, p.furnished, p.garage, p.has_lift,
           p.building_type, ll.obec_kod, ll.katastr_kod, ll.country_status) mf on true
  where p.status = 'active'::text;

  revoke all on public.properties_public from anon, authenticated;
  grant select on public.properties_public to authenticated;

  -- 508 section 1f / 584's body, verbatim.
  create view pipeline_board_public
  with (security_invoker = true) as
  select
    pp.property_id,
    pp.stage_id,
    pp.board_position,
    pp.entered_stage_at,
    pp.added_at,
    p.sreality_id,
    p.source,
    p.source_id_native,
    p.listing_id,
    p.category_main,
    p.disposition,
    p.subtype,
    p.area_m2,
    p.price_czk,
    p.mf_gross_yield_pct,
    p.total_price_change_pct,
    p.price_change_count,
    p.obec_id,
    p.okres_id,
    p.region_id,
    p.obec,
    p.is_active,
    p.category_type,
    p.price_per_m2,
    p.price_per_m2_basis,
    p.display_label,
    p.cast_obce_id,
    p.ulice_id
  from property_pipeline_public pp
  left join properties_public p on p.property_id = pp.property_id;

  revoke all on public.pipeline_board_public from public, anon, authenticated;
  grant select on public.pipeline_board_public to authenticated;
end
$tx1$;

-- ---------------------------------------------------------------------------
-- 2b. rebuild_browse_list(): 522's body (read live: md5 1386c849...) plus nine static index lines
--     after the region cover and nine renames after its rename (a tenth portal needs one of each; a
--     test fails until then). After TX1, never before: the old body over the new projection still
--     publishes a valid list should a crash let pg_cron tick in between, the new body over the old
--     one fails. Outside the DO block: the receipt parser (scripts/migration_objects.py) reads a
--     body inside one as apply-time DDL and would probe the rebuild's own scratch table.
-- ---------------------------------------------------------------------------
create or replace function public.rebuild_browse_list()
returns void
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
  t0 timestamptz := clock_timestamp();
  n  bigint;
begin
  -- Transaction-scoped: released by commit, rollback OR cancel (W13).
  if not pg_try_advisory_xact_lock(hashtext('rebuild_browse_list')) then
    raise notice 'rebuild_browse_list: previous run still active, skipping tick';
    return;
  end if;

  execute 'drop table if exists browse_list_next';
  execute $q$
    create unlogged table browse_list_next as
    select * from browse_projection
    order by category_main, category_type, first_seen_at
  $q$;
  execute 'create unique index browse_list_next_pk on browse_list_next (property_id)';
  execute 'create index browse_list_next_cat_first_seen_idx on browse_list_next (category_main, category_type, first_seen_at desc, property_id desc)';
  execute 'create index browse_list_next_obec_price_idx on browse_list_next (obec_id, category_type, price_czk, property_id, category_main, subtype, disposition, area_m2, is_active) where obec_id is not null';
  execute 'create index browse_list_next_okres_price_idx on browse_list_next (okres_id, category_type, price_czk, property_id, category_main, subtype, disposition, area_m2, is_active) where okres_id is not null';
  execute 'create index browse_list_next_region_price_idx on browse_list_next (region_id, category_type, price_czk, property_id, category_main, subtype, disposition, area_m2, is_active) where region_id is not null';
  execute 'create index browse_list_next_newest_ad_at_sreality_idx on browse_list_next (category_main, category_type, newest_ad_at_sreality desc, property_id desc) where newest_ad_at_sreality is not null';
  execute 'create index browse_list_next_newest_ad_at_bazos_idx on browse_list_next (category_main, category_type, newest_ad_at_bazos desc, property_id desc) where newest_ad_at_bazos is not null';
  execute 'create index browse_list_next_newest_ad_at_idnes_idx on browse_list_next (category_main, category_type, newest_ad_at_idnes desc, property_id desc) where newest_ad_at_idnes is not null';
  execute 'create index browse_list_next_newest_ad_at_maxima_idx on browse_list_next (category_main, category_type, newest_ad_at_maxima desc, property_id desc) where newest_ad_at_maxima is not null';
  execute 'create index browse_list_next_newest_ad_at_ceskereality_idx on browse_list_next (category_main, category_type, newest_ad_at_ceskereality desc, property_id desc) where newest_ad_at_ceskereality is not null';
  execute 'create index browse_list_next_newest_ad_at_bezrealitky_idx on browse_list_next (category_main, category_type, newest_ad_at_bezrealitky desc, property_id desc) where newest_ad_at_bezrealitky is not null';
  execute 'create index browse_list_next_newest_ad_at_mmreality_idx on browse_list_next (category_main, category_type, newest_ad_at_mmreality desc, property_id desc) where newest_ad_at_mmreality is not null';
  execute 'create index browse_list_next_newest_ad_at_remax_idx on browse_list_next (category_main, category_type, newest_ad_at_remax desc, property_id desc) where newest_ad_at_remax is not null';
  execute 'create index browse_list_next_newest_ad_at_realitymix_idx on browse_list_next (category_main, category_type, newest_ad_at_realitymix desc, property_id desc) where newest_ad_at_realitymix is not null';
  execute 'analyze browse_list_next';
  execute 'select count(*) from browse_list_next' into n;

  execute 'drop table if exists browse_list';
  execute 'alter table browse_list_next rename to browse_list';
  execute 'alter index browse_list_next_pk rename to browse_list_pk';
  execute 'alter index browse_list_next_cat_first_seen_idx rename to browse_list_cat_first_seen_idx';
  execute 'alter index browse_list_next_obec_price_idx rename to browse_list_obec_price_idx';
  execute 'alter index browse_list_next_okres_price_idx rename to browse_list_okres_price_idx';
  execute 'alter index browse_list_next_region_price_idx rename to browse_list_region_price_idx';
  execute 'alter index browse_list_next_newest_ad_at_sreality_idx rename to browse_list_newest_ad_at_sreality_idx';
  execute 'alter index browse_list_next_newest_ad_at_bazos_idx rename to browse_list_newest_ad_at_bazos_idx';
  execute 'alter index browse_list_next_newest_ad_at_idnes_idx rename to browse_list_newest_ad_at_idnes_idx';
  execute 'alter index browse_list_next_newest_ad_at_maxima_idx rename to browse_list_newest_ad_at_maxima_idx';
  execute 'alter index browse_list_next_newest_ad_at_ceskereality_idx rename to browse_list_newest_ad_at_ceskereality_idx';
  execute 'alter index browse_list_next_newest_ad_at_bezrealitky_idx rename to browse_list_newest_ad_at_bezrealitky_idx';
  execute 'alter index browse_list_next_newest_ad_at_mmreality_idx rename to browse_list_newest_ad_at_mmreality_idx';
  execute 'alter index browse_list_next_newest_ad_at_remax_idx rename to browse_list_newest_ad_at_remax_idx';
  execute 'alter index browse_list_next_newest_ad_at_realitymix_idx rename to browse_list_newest_ad_at_realitymix_idx';
  execute 'grant select on browse_list to authenticated';
  execute 'revoke insert, update, delete, truncate on browse_list from anon, authenticated';

  if has_table_privilege('anon', 'browse_list', 'SELECT') then
    raise exception 'rebuild_browse_list: anon must never hold SELECT on browse_list -- refusing to publish this rebuild (see migration 374)';
  end if;

  update derived_artifacts
     set last_succeeded_at = now(),
         complete_through  = now(),
         last_duration_ms  = (extract(epoch from clock_timestamp() - t0) * 1000)::integer,
         last_rows         = n
   where name = 'browse_list';
  perform pg_notify('pgrst', 'reload schema');
end
$function$;

revoke execute on function public.rebuild_browse_list() from public, anon, authenticated;
grant execute on function public.rebuild_browse_list() to service_role;

-- ---------------------------------------------------------------------------
-- 3. The list, rebuilt by the function pg_cron runs (this session holds its re-entrant key, so it
--    RUNS rather than skips): the lists, the count and the dates filled, the nine indexes built.
--    lock_timeout 0 (522 section 4: a lock timeout would throw away a finished rebuild at its final
--    rename).
-- ---------------------------------------------------------------------------
set lock_timeout = 0;
set statement_timeout = '3600s';

select public.rebuild_browse_list();

-- ---------------------------------------------------------------------------
-- 4. TX2: the map rebuilt the same way; its source back to 561's body; the legacy projection
--    dropped (the old matview, its last dependant, went in the swap); both aggregate RPCs switched
--    to the portal rule in the same commit, now that browse_list and properties_map_mv carry it.
-- ---------------------------------------------------------------------------
begin;

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

drop view if exists public.browse_projection_legacy;

-- 549's two bodies (= the live prosrc, md5 cbbc44a4... / 5af4e22f...) with exactly two edits each:
-- the two status lines become one portal_status_matches() call, and the canonical-portal line
-- (`l.source = any(portal_filter)`) goes. New md5s: f4d95769... / 0a1cf3a9....
CREATE OR REPLACE FUNCTION public.browse_stats_properties(districts_filter text[] DEFAULT NULL::text[], dispositions_filter text[] DEFAULT NULL::text[], price_min_filter integer DEFAULT NULL::integer, price_max_filter integer DEFAULT NULL::integer, area_min_filter integer DEFAULT NULL::integer, area_max_filter integer DEFAULT NULL::integer, active_only_filter boolean DEFAULT false, last_seen_min_days integer DEFAULT NULL::integer, last_seen_max_days integer DEFAULT NULL::integer, first_seen_min_days integer DEFAULT NULL::integer, first_seen_max_days integer DEFAULT NULL::integer, tom_days_min integer DEFAULT NULL::integer, tom_days_max integer DEFAULT NULL::integer, has_balcony_filter boolean DEFAULT NULL::boolean, has_lift_filter boolean DEFAULT NULL::boolean, has_parking_filter boolean DEFAULT NULL::boolean, inactive_only_filter boolean DEFAULT false, furnished_filter text[] DEFAULT NULL::text[], terrace_filter boolean DEFAULT NULL::boolean, cellar_filter boolean DEFAULT NULL::boolean, garage_filter boolean DEFAULT NULL::boolean, category_sub_cb_filter integer DEFAULT NULL::integer, building_type_filter text[] DEFAULT NULL::text[], tag_ids bigint[] DEFAULT NULL::bigint[], category_main_filter text[] DEFAULT NULL::text[], category_type_filter text DEFAULT NULL::text, bbox_west double precision DEFAULT NULL::double precision, bbox_south double precision DEFAULT NULL::double precision, bbox_east double precision DEFAULT NULL::double precision, bbox_north double precision DEFAULT NULL::double precision, ownership_filter text[] DEFAULT NULL::text[], estate_area_min_filter double precision DEFAULT NULL::double precision, estate_area_max_filter double precision DEFAULT NULL::double precision, usable_area_min_filter double precision DEFAULT NULL::double precision, usable_area_max_filter double precision DEFAULT NULL::double precision, parking_lots_min_filter integer DEFAULT NULL::integer, garden_area_min_filter double precision DEFAULT NULL::double precision, garden_area_max_filter double precision DEFAULT NULL::double precision, condition_match_filter text[] DEFAULT NULL::text[], districts_context_filter text[] DEFAULT NULL::text[], city_index_rules jsonb DEFAULT NULL::jsonb, city_pop_min integer DEFAULT NULL::integer, city_pop_max integer DEFAULT NULL::integer, city_proximity jsonb DEFAULT NULL::jsonb, price_per_m2_min double precision DEFAULT NULL::double precision, price_per_m2_max double precision DEFAULT NULL::double precision, portal_filter text[] DEFAULT NULL::text[], mf_gross_yield_pct_min double precision DEFAULT NULL::double precision, mf_gross_yield_pct_max double precision DEFAULT NULL::double precision, near_pop_5km_min integer DEFAULT NULL::integer, near_pop_15km_min integer DEFAULT NULL::integer, near_jobs_5km_min double precision DEFAULT NULL::double precision, near_jobs_15km_min double precision DEFAULT NULL::double precision, near_youth_5km_min double precision DEFAULT NULL::double precision, near_youth_15km_min double precision DEFAULT NULL::double precision, near_overall_5km_min double precision DEFAULT NULL::double precision, near_overall_15km_min double precision DEFAULT NULL::double precision, districts_excluded_filter boolean[] DEFAULT NULL::boolean[], subtype_filter text[] DEFAULT NULL::text[], recently_added_days integer DEFAULT NULL::integer, recently_changed_days integer DEFAULT NULL::integer, obec_ids_filter bigint[] DEFAULT NULL::bigint[], districts_levels text[] DEFAULT NULL::text[], districts_ids bigint[] DEFAULT NULL::bigint[], building_condition_level_min integer DEFAULT NULL::integer, building_condition_level_max integer DEFAULT NULL::integer, apartment_condition_level_min integer DEFAULT NULL::integer, apartment_condition_level_max integer DEFAULT NULL::integer, price_change_count_min integer DEFAULT NULL::integer, price_change_window_days integer DEFAULT NULL::integer, total_price_change_pct_filter double precision DEFAULT NULL::double precision, with_estimates boolean DEFAULT false, include_no_price boolean DEFAULT false, property_ids_filter bigint[] DEFAULT NULL::bigint[], hide_dismissed boolean DEFAULT false)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
begin
  if city_proximity is not null then
    raise exception using errcode = '22023',
      message = 'browse_stats_properties: city_proximity is retired (W5, migration 436). '
                'Use the migration-142 near_*_min columns.';
  end if;
  return (
  with filtered as (
    select l.sreality_id, l.first_seen_at, l.last_seen_at, l.is_active, l.price_czk, l.area_m2, l.disposition, l.tom_days, l.price_per_m2, l.category_main, l.category_type
    from browse_list l
    where
          public.portal_status_matches(l.is_active, l.all_sources, l.active_sources, portal_filter,
            case when active_only_filter then 'active' when inactive_only_filter then 'inactive' else 'any' end)
      and (last_seen_max_days is null or l.last_seen_at >= now() - (last_seen_max_days || ' days')::interval)
      and (last_seen_min_days is null or l.last_seen_at <= now() - (last_seen_min_days || ' days')::interval)
      and (first_seen_max_days is null or l.first_seen_at >= now() - (first_seen_max_days || ' days')::interval)
      and (first_seen_min_days is null or l.first_seen_at <= now() - (first_seen_min_days || ' days')::interval)
      and (recently_added_days   is null or l.first_seen_at  >= now() - (recently_added_days   || ' days')::interval)
      and (recently_changed_days is null or l.last_change_at >= now() - (recently_changed_days || ' days')::interval)
      and (tom_days_min is null or l.tom_days >= tom_days_min)
      and (tom_days_max is null or l.tom_days <= tom_days_max)
      and (category_main_filter   is null or array_length(category_main_filter, 1) is null or l.category_main = any(category_main_filter))
      and (category_type_filter   is null or l.category_type   = category_type_filter)
      and (
        districts_filter is null or array_length(districts_filter, 1) is null
        or not exists (
          select 1 from unnest(districts_filter,
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, excl, ord)
          where not coalesce(excl, false)
        )
        or exists (
          select 1 from unnest(districts_filter,
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)])),
                 coalesce(districts_levels, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_ids, array_fill(null::bigint, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, excl, lvl, admin_id, ord)
          where not coalesce(excl, false)
            and case
              -- ONE code predicate (W3 S3): a chip is a level plus a RUIAN code,
              -- matched with plain equality. No ILIKE, no place_search_text, no
              -- context narrow. A chip with no code matches NOTHING (the `else`
              -- and the null guard) -- a saved filter we cannot resolve must
              -- never widen a cohort.
              when admin_id is null                  then false
              when lvl = 'kraj'                      then l.region_id    = admin_id
              when lvl = 'okres'                     then l.okres_id     = admin_id
              when lvl = 'obec'                      then l.obec_id      = admin_id
              when lvl = 'cast_obce'                 then l.cast_obce_id = admin_id
              -- a street / POI / address pick carries its containing obec code
              when lvl = 'locality'                  then l.obec_id      = admin_id
              else false
            end
        )
      )
      and (
        districts_filter is null or array_length(districts_filter, 1) is null
        or not exists (
          select 1 from unnest(districts_filter,
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)])),
                 coalesce(districts_levels, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_ids, array_fill(null::bigint, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, excl, lvl, admin_id, ord)
          where coalesce(excl, false)
            and case
              -- ONE code predicate (W3 S3): a chip is a level plus a RUIAN code,
              -- matched with plain equality. No ILIKE, no place_search_text, no
              -- context narrow. A chip with no code matches NOTHING (the `else`
              -- and the null guard) -- a saved filter we cannot resolve must
              -- never widen a cohort.
              when admin_id is null                  then false
              when lvl = 'kraj'                      then l.region_id    = admin_id
              when lvl = 'okres'                     then l.okres_id     = admin_id
              when lvl = 'obec'                      then l.obec_id      = admin_id
              when lvl = 'cast_obce'                 then l.cast_obce_id = admin_id
              -- a street / POI / address pick carries its containing obec code
              when lvl = 'locality'                  then l.obec_id      = admin_id
              else false
            end
        )
      )
      and (dispositions_filter    is null or l.disposition     = any(dispositions_filter))
      and (price_min_filter       is null or (include_no_price and l.price_czk is null) or l.price_czk >= price_min_filter)
      and (price_max_filter       is null or (include_no_price and l.price_czk is null) or l.price_czk <= price_max_filter)
      and (area_min_filter        is null or l.area_m2        >= area_min_filter)
      and (area_max_filter        is null or l.area_m2        <= area_max_filter)
      and (price_per_m2_min is null or l.price_per_m2 >= price_per_m2_min)
      and (price_per_m2_max is null or l.price_per_m2 <= price_per_m2_max)
      and (mf_gross_yield_pct_min is null or l.mf_gross_yield_pct >= mf_gross_yield_pct_min)
      and (mf_gross_yield_pct_max is null or l.mf_gross_yield_pct <= mf_gross_yield_pct_max)
      and (has_balcony_filter     is null or l.has_balcony     = has_balcony_filter)
      and (has_lift_filter        is null or l.has_lift        = has_lift_filter)
      and (has_parking_filter     is null or l.has_parking     = has_parking_filter)
      and (
        furnished_filter is null or array_length(furnished_filter, 1) is null
        or l.furnished = any(furnished_filter)
        or ('__unknown__' = any(furnished_filter)
            and (l.furnished is null or not (l.furnished = any(array['ano','ne','castecne']))))
      )
      and (terrace_filter         is null or l.terrace         = terrace_filter)
      and (cellar_filter          is null or l.cellar          = cellar_filter)
      and (garage_filter          is null or l.garage          = garage_filter)
      and (category_sub_cb_filter is null or l.category_sub_cb = category_sub_cb_filter)
      and (subtype_filter is null or array_length(subtype_filter, 1) is null or l.subtype = any(subtype_filter))
      and (building_type_filter   is null or array_length(building_type_filter, 1) is null or l.building_type = any(building_type_filter))
      and (condition_match_filter is null or array_length(condition_match_filter, 1) is null or l.condition = any(condition_match_filter))
      and (
        ownership_filter is null or array_length(ownership_filter, 1) is null
        or l.ownership = any(ownership_filter)
        or ('__unknown__' = any(ownership_filter)
            and (l.ownership is null or not (l.ownership = any(array['osobni','druzstevni','statni','jine']))))
      )
      and (estate_area_min_filter  is null or l.plot_area_m2  >= estate_area_min_filter)
      and (estate_area_max_filter  is null or l.plot_area_m2  <= estate_area_max_filter)
      and (usable_area_min_filter  is null or l.usable_area   >= usable_area_min_filter)
      and (usable_area_max_filter  is null or l.usable_area   <= usable_area_max_filter)
      and (parking_lots_min_filter is null or l.parking_lots  >= parking_lots_min_filter)
      and (garden_area_min_filter  is null or l.garden_area   >= garden_area_min_filter)
      and (garden_area_max_filter  is null or l.garden_area   <= garden_area_max_filter)
      and (bbox_west  is null or l.lng >= bbox_west)
      and (bbox_east  is null or l.lng <= bbox_east)
      and (bbox_south is null or l.lat >= bbox_south)
      and (bbox_north is null or l.lat <= bbox_north)
      and (building_condition_level_min  is null or l.building_condition_level  >= building_condition_level_min)
      and (building_condition_level_max  is null or l.building_condition_level  <= building_condition_level_max)
      and (apartment_condition_level_min is null or l.apartment_condition_level >= apartment_condition_level_min)
      and (apartment_condition_level_max is null or l.apartment_condition_level <= apartment_condition_level_max)
      and (price_change_count_min is null or
           (case when price_change_window_days = 30  then l.price_change_count_30d
                 when price_change_window_days = 90  then l.price_change_count_90d
                 when price_change_window_days = 365 then l.price_change_count_365d
                 else l.price_change_count end) >= price_change_count_min)
      and (total_price_change_pct_filter is null or total_price_change_pct_filter = 0
           or (total_price_change_pct_filter < 0 and l.total_price_change_pct <= total_price_change_pct_filter)
           or (total_price_change_pct_filter > 0 and l.total_price_change_pct >= total_price_change_pct_filter))
      and (not coalesce(with_estimates, false) or exists (
            select 1 from property_estimates_public pe where pe.property_id = l.property_id))
      and (obec_ids_filter is null or l.obec_id = any(obec_ids_filter))
      and (property_ids_filter is null or l.property_id = any(property_ids_filter))
      and (not hide_dismissed or not exists (
            select 1 from property_dismissals_public d where d.property_id = l.property_id))
      and (tag_ids is null or array_length(tag_ids, 1) is null or l.property_id in (
          select pt.property_id from property_tags pt where pt.tag_id = any(tag_ids)
          group by pt.property_id having count(distinct pt.tag_id) = array_length(tag_ids, 1)))
      and (city_pop_min is null or l.home_obec_pop >= city_pop_min)
      and (city_pop_max is null or l.home_obec_pop <= city_pop_max)
      and (near_pop_5km_min      is null or l.near_pop_5km      >= near_pop_5km_min)
      and (near_pop_15km_min     is null or l.near_pop_15km     >= near_pop_15km_min)
      and (near_jobs_5km_min     is null or l.near_jobs_5km     >= near_jobs_5km_min)
      and (near_jobs_15km_min    is null or l.near_jobs_15km    >= near_jobs_15km_min)
      and (near_youth_5km_min    is null or l.near_youth_5km    >= near_youth_5km_min)
      and (near_youth_15km_min   is null or l.near_youth_15km   >= near_youth_15km_min)
      and (near_overall_5km_min  is null or l.near_overall_5km  >= near_overall_5km_min)
      and (near_overall_15km_min is null or l.near_overall_15km >= near_overall_15km_min)
      and (city_index_rules is null or jsonb_array_length(city_index_rules) = 0
           or l.obec_id = any (array(select curated_cities_matching(city_index_rules))))
      and true
  ),
  price_pct as (select percentile_cont(0.25) within group (order by price_czk)::int as p25, percentile_cont(0.50) within group (order by price_czk)::int as p50, percentile_cont(0.75) within group (order by price_czk)::int as p75 from filtered where price_czk is not null),
  ppm2_pct as (select percentile_cont(0.25) within group (order by price_per_m2)::int as p25, percentile_cont(0.50) within group (order by price_per_m2)::int as p50, percentile_cont(0.75) within group (order by price_per_m2)::int as p75 from filtered where price_per_m2 is not null),
  ppm2_basis as (select case when count(distinct b) = 0 then null when count(distinct b) = 1 then min(b) else 'mixed' end as basis from (select measure_price_per_m2_basis(category_main, category_type) as b from filtered where price_per_m2 is not null) t),
  disposition_dist as (select coalesce(disposition, 'unspecified') as disposition, count(*)::int as n, count(price_per_m2)::int as ppm2_n, min(price_per_m2)::int as ppm2_min, percentile_cont(0.25) within group (order by price_per_m2)::int as ppm2_p25, percentile_cont(0.50) within group (order by price_per_m2)::int as ppm2_median, percentile_cont(0.75) within group (order by price_per_m2)::int as ppm2_p75, max(price_per_m2)::int as ppm2_max from filtered group by disposition order by n desc, disposition asc),
  price_cuts as (select percentile_cont(0.10) within group (order by price_czk) as cut_10, percentile_cont(0.25) within group (order by price_czk) as cut_25, percentile_cont(0.45) within group (order by price_czk) as cut_45, percentile_cont(0.55) within group (order by price_czk) as cut_55, percentile_cont(0.75) within group (order by price_czk) as cut_75, percentile_cont(0.90) within group (order by price_czk) as cut_90, count(*)::int as priced_total from filtered where price_czk is not null),
  price_bands as (select f.price_czk, f.tom_days, case when f.price_czk <= c.cut_10 then 1 when f.price_czk <= c.cut_25 then 2 when f.price_czk <= c.cut_45 then 3 when f.price_czk <= c.cut_55 then 4 when f.price_czk <= c.cut_75 then 5 when f.price_czk <= c.cut_90 then 6 else 7 end as bucket, c.priced_total from filtered f, price_cuts c where f.price_czk is not null),
  band_definitions(bucket, p_lo, p_hi) as (values (1, 0, 10), (2, 10, 25), (3, 25, 45), (4, 45, 55), (5, 55, 75), (6, 75, 90), (7, 90, 100)),
  band_stats as (select d.bucket, d.p_lo, d.p_hi, count(b.price_czk)::int as n, max(b.priced_total) as priced_total, min(b.price_czk)::int as price_min, max(b.price_czk)::int as price_max, count(b.tom_days)::int as tom_n, min(b.tom_days)::int as tom_min, percentile_cont(0.25) within group (order by b.tom_days) filter (where b.tom_days is not null) as tom_p25, percentile_cont(0.50) within group (order by b.tom_days) filter (where b.tom_days is not null) as tom_median, percentile_cont(0.75) within group (order by b.tom_days) filter (where b.tom_days is not null) as tom_p75, max(b.tom_days)::int as tom_max, avg(b.tom_days) filter (where b.tom_days is not null) as tom_mean from band_definitions d left join price_bands b on b.bucket = d.bucket group by d.bucket, d.p_lo, d.p_hi order by d.bucket)
  select jsonb_build_object(
    'total', (select count(*)::int from filtered),
    'new_7d', (select count(*)::int from filtered where first_seen_at >= now() - interval '7 days'),
    'new_30d', (select count(*)::int from filtered where first_seen_at >= now() - interval '30 days'),
    'price', (select case when p50 is null then null else jsonb_build_object('p25', p25, 'p50', p50, 'p75', p75) end from price_pct),
    'ppm2', (select case when p50 is null then null else jsonb_build_object('p25', p25, 'p50', p50, 'p75', p75) end from ppm2_pct),
    'ppm2_basis', (select basis from ppm2_basis),
    'dispositions', coalesce((select jsonb_agg(jsonb_build_object('disposition', disposition, 'n', n, 'ppm2_box', case when ppm2_n > 0 then jsonb_build_object('n', ppm2_n, 'min', ppm2_min, 'p25', ppm2_p25, 'median', ppm2_median, 'p75', ppm2_p75, 'max', ppm2_max) else null end)) from disposition_dist), '[]'::jsonb),
    'price_band_velocity', coalesce((select jsonb_agg(jsonb_build_object('bucket', bs.bucket, 'p_lo', bs.p_lo, 'p_hi', bs.p_hi, 'n', bs.n, 'pct_share', case when bs.priced_total is null or bs.priced_total = 0 then null else round(bs.n * 100.0 / bs.priced_total, 1) end, 'price_min', bs.price_min, 'price_max', bs.price_max, 'tom_box', case when bs.tom_n > 0 then jsonb_build_object('n', bs.tom_n, 'min', bs.tom_min, 'p25', round(bs.tom_p25::numeric, 1), 'median', round(bs.tom_median::numeric, 1), 'mean', round(bs.tom_mean::numeric, 1), 'p75', round(bs.tom_p75::numeric, 1), 'max', bs.tom_max) else null end) order by bs.p_lo) from band_stats bs), '[]'::jsonb)
  )
  );
end
$function$;

create or replace function public.browse_map_cells(
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
  furnished_filter text[] default null::text[],
  terrace_filter boolean default null::boolean,
  cellar_filter boolean default null::boolean,
  garage_filter boolean default null::boolean,
  category_sub_cb_filter integer default null::integer,
  building_type_filter text[] default null::text[],
  tag_ids bigint[] default null::bigint[],
  category_main_filter text[] default null::text[],
  category_type_filter text default null::text,
  bbox_west double precision default null::double precision,
  bbox_south double precision default null::double precision,
  bbox_east double precision default null::double precision,
  bbox_north double precision default null::double precision,
  ownership_filter text[] default null::text[],
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
  portal_filter text[] default null::text[],
  mf_gross_yield_pct_min double precision default null::double precision,
  mf_gross_yield_pct_max double precision default null::double precision,
  near_pop_5km_min integer default null::integer,
  near_pop_15km_min integer default null::integer,
  near_jobs_5km_min double precision default null::double precision,
  near_jobs_15km_min double precision default null::double precision,
  near_youth_5km_min double precision default null::double precision,
  near_youth_15km_min double precision default null::double precision,
  near_overall_5km_min double precision default null::double precision,
  near_overall_15km_min double precision default null::double precision,
  districts_excluded_filter boolean[] default null::boolean[],
  subtype_filter text[] default null::text[],
  recently_added_days integer default null::integer,
  recently_changed_days integer default null::integer,
  obec_ids_filter bigint[] default null::bigint[],
  districts_levels text[] default null::text[],
  districts_ids bigint[] default null::bigint[],
  building_condition_level_min integer default null::integer,
  building_condition_level_max integer default null::integer,
  apartment_condition_level_min integer default null::integer,
  apartment_condition_level_max integer default null::integer,
  price_change_count_min integer default null::integer,
  price_change_window_days integer default null::integer,
  total_price_change_pct_filter double precision default null::double precision,
  with_estimates boolean default false,
  include_no_price boolean default false,
  property_ids_filter bigint[] default null::bigint[],
  -- The two parameters browse_stats_properties does not have.
  --
  -- listing_ids_filter: the SPA's applyPrefilters emits `.in()` on THREE id
  -- spaces -- listing_id, obec_id and property_id. browse_stats_properties
  -- carries only the last two because the legacy city-quality path reaches it
  -- as city_index_rules instead. The map resolves that path client-side into a
  -- listing_id allowlist (queries.ts resolveCityQualityPrefilterLegacy, live
  -- whenever ?cityQualityLegacy=1 is remembered in localStorage), so without
  -- this parameter the RPC would silently drop a prefilter that the read it
  -- replaces applies.
  listing_ids_filter bigint[] default null::bigint[],
  -- Above this many mappable rows the answer is cells; at or below it the
  -- caller re-reads the cohort as points. Measured: a 2,000-row point read is
  -- 153 blocks and ~906 KB, which is the ceiling this threshold buys.
  point_budget integer default 2000,
  -- migration 537: exclude the caller's dismissed properties (RLS-scoped).
  hide_dismissed boolean default false
)
returns jsonb
language plpgsql
stable
-- 74 mostly-NULL parameters: a generic plan would pick one shape and use it for
-- every cohort. Same setting, same reason, as browse_stats_properties.
set plan_cache_mode to 'force_custom_plan'
as $function$
declare
  -- The Czech Republic, used only when the cohort carries no bbox of its own.
  -- Real CZ bounds are lng 12.09..18.86, lat 48.55..51.06.
  c_cz_west  constant double precision := 12.0;
  c_cz_east  constant double precision := 18.9;
  c_cz_south constant double precision := 48.5;
  c_cz_north constant double precision := 51.1;
  c_cols constant integer := 20;
  c_rows constant integer := 13;
  v_w double precision;
  v_e double precision;
  v_s double precision;
  v_n double precision;
  v_cw double precision;
  v_ch double precision;
  v_result jsonb;
  v_total bigint;
begin
  if city_proximity is not null then
    raise exception using errcode = '22023',
      message = 'browse_map_cells: city_proximity is retired (W5, migration 436). '
                'Use the migration-142 near_*_min columns.';
  end if;

  v_w := coalesce(bbox_west,  c_cz_west);
  v_e := coalesce(bbox_east,  c_cz_east);
  v_s := coalesce(bbox_south, c_cz_south);
  v_n := coalesce(bbox_north, c_cz_north);
  -- A degenerate extent (a point bbox, or an inverted one from a bad URL) would
  -- divide by zero or produce a negative cell; fall back to the CZ box rather
  -- than raise, because the cohort predicate is still perfectly well defined.
  if not (v_e > v_w and v_n > v_s) then
    v_w := c_cz_west; v_e := c_cz_east; v_s := c_cz_south; v_n := c_cz_north;
  end if;
  v_cw := (v_e - v_w) / c_cols;
  v_ch := (v_n - v_s) / c_rows;

  with g as (
    select
      -- cx/cy are NULL together for a row outside the grid extent. Those rows
      -- collapse into ONE group, are reported as off_grid, and are still inside
      -- total -- never relocated onto an edge cell, never dropped.
      case when l.lat >= v_s and l.lat <= v_n and l.lng >= v_w and l.lng <= v_e
           then least(floor((l.lng - v_w) / v_cw)::int, c_cols - 1) end as cx,
      case when l.lat >= v_s and l.lat <= v_n and l.lng >= v_w and l.lng <= v_e
           then least(floor((l.lat - v_s) / v_ch)::int, c_rows - 1) end as cy,
      count(*)::int as n,
      -- The MEAN of the points in the cell, not the cell centre: the bubble
      -- then sits on the settlement instead of on a lattice vertex. Both
      -- columns are index keys, so this costs nothing and keeps Heap Fetches 0.
      avg(l.lat) as la,
      avg(l.lng) as lo
    from properties_map_mv l
    where
          public.portal_status_matches(l.is_active, l.all_sources, l.active_sources, portal_filter,
            case when active_only_filter then 'active' when inactive_only_filter then 'inactive' else 'any' end)
      and (last_seen_max_days is null or l.last_seen_at >= now() - (last_seen_max_days || ' days')::interval)
      and (last_seen_min_days is null or l.last_seen_at <= now() - (last_seen_min_days || ' days')::interval)
      and (first_seen_max_days is null or l.first_seen_at >= now() - (first_seen_max_days || ' days')::interval)
      and (first_seen_min_days is null or l.first_seen_at <= now() - (first_seen_min_days || ' days')::interval)
      and (recently_added_days   is null or l.first_seen_at  >= now() - (recently_added_days   || ' days')::interval)
      and (recently_changed_days is null or l.last_change_at >= now() - (recently_changed_days || ' days')::interval)
      and (tom_days_min is null or l.tom_days >= tom_days_min)
      and (tom_days_max is null or l.tom_days <= tom_days_max)
      and (category_main_filter   is null or array_length(category_main_filter, 1) is null or l.category_main = any(category_main_filter))
      and (category_type_filter   is null or l.category_type   = category_type_filter)
      and (
        districts_filter is null or array_length(districts_filter, 1) is null
        or not exists (
          select 1 from unnest(districts_filter,
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, excl, ord)
          where not coalesce(excl, false)
        )
        or exists (
          select 1 from unnest(districts_filter,
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)])),
                 coalesce(districts_levels, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_ids, array_fill(null::bigint, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, excl, lvl, admin_id, ord)
          where not coalesce(excl, false)
            and case
              -- ONE code predicate (W3 S3): a chip is a level plus a RUIAN code,
              -- matched with plain equality. No ILIKE, no place_search_text, no
              -- context narrow. A chip with no code matches NOTHING (the `else`
              -- and the null guard) -- a saved filter we cannot resolve must
              -- never widen a cohort.
              when admin_id is null                  then false
              when lvl = 'kraj'                      then l.region_id    = admin_id
              when lvl = 'okres'                     then l.okres_id     = admin_id
              when lvl = 'obec'                      then l.obec_id      = admin_id
              when lvl = 'cast_obce'                 then l.cast_obce_id = admin_id
              -- a street / POI / address pick carries its containing obec code
              when lvl = 'locality'                  then l.obec_id      = admin_id
              else false
            end
        )
      )
      and (
        districts_filter is null or array_length(districts_filter, 1) is null
        or not exists (
          select 1 from unnest(districts_filter,
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)])),
                 coalesce(districts_levels, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_ids, array_fill(null::bigint, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, excl, lvl, admin_id, ord)
          where coalesce(excl, false)
            and case
              -- ONE code predicate (W3 S3): a chip is a level plus a RUIAN code,
              -- matched with plain equality. No ILIKE, no place_search_text, no
              -- context narrow. A chip with no code matches NOTHING (the `else`
              -- and the null guard) -- a saved filter we cannot resolve must
              -- never widen a cohort.
              when admin_id is null                  then false
              when lvl = 'kraj'                      then l.region_id    = admin_id
              when lvl = 'okres'                     then l.okres_id     = admin_id
              when lvl = 'obec'                      then l.obec_id      = admin_id
              when lvl = 'cast_obce'                 then l.cast_obce_id = admin_id
              -- a street / POI / address pick carries its containing obec code
              when lvl = 'locality'                  then l.obec_id      = admin_id
              else false
            end
        )
      )
      and (dispositions_filter    is null or l.disposition     = any(dispositions_filter))
      and (price_min_filter       is null or (include_no_price and l.price_czk is null) or l.price_czk >= price_min_filter)
      and (price_max_filter       is null or (include_no_price and l.price_czk is null) or l.price_czk <= price_max_filter)
      and (area_min_filter        is null or l.area_m2        >= area_min_filter)
      and (area_max_filter        is null or l.area_m2        <= area_max_filter)
      and (price_per_m2_min is null or l.price_per_m2 >= price_per_m2_min)
      and (price_per_m2_max is null or l.price_per_m2 <= price_per_m2_max)
      and (mf_gross_yield_pct_min is null or l.mf_gross_yield_pct >= mf_gross_yield_pct_min)
      and (mf_gross_yield_pct_max is null or l.mf_gross_yield_pct <= mf_gross_yield_pct_max)
      and (has_balcony_filter     is null or l.has_balcony     = has_balcony_filter)
      and (has_lift_filter        is null or l.has_lift        = has_lift_filter)
      and (has_parking_filter     is null or l.has_parking     = has_parking_filter)
      and (
        furnished_filter is null or array_length(furnished_filter, 1) is null
        or l.furnished = any(furnished_filter)
        or ('__unknown__' = any(furnished_filter)
            and (l.furnished is null or not (l.furnished = any(array['ano','ne','castecne']))))
      )
      and (terrace_filter         is null or l.terrace         = terrace_filter)
      and (cellar_filter          is null or l.cellar          = cellar_filter)
      and (garage_filter          is null or l.garage          = garage_filter)
      and (category_sub_cb_filter is null or l.category_sub_cb = category_sub_cb_filter)
      and (subtype_filter is null or array_length(subtype_filter, 1) is null or l.subtype = any(subtype_filter))
      and (building_type_filter   is null or array_length(building_type_filter, 1) is null or l.building_type = any(building_type_filter))
      and (condition_match_filter is null or array_length(condition_match_filter, 1) is null or l.condition = any(condition_match_filter))
      and (
        ownership_filter is null or array_length(ownership_filter, 1) is null
        or l.ownership = any(ownership_filter)
        or ('__unknown__' = any(ownership_filter)
            and (l.ownership is null or not (l.ownership = any(array['osobni','druzstevni','statni','jine']))))
      )
      and (estate_area_min_filter  is null or l.plot_area_m2  >= estate_area_min_filter)
      and (estate_area_max_filter  is null or l.plot_area_m2  <= estate_area_max_filter)
      and (usable_area_min_filter  is null or l.usable_area   >= usable_area_min_filter)
      and (usable_area_max_filter  is null or l.usable_area   <= usable_area_max_filter)
      and (parking_lots_min_filter is null or l.parking_lots  >= parking_lots_min_filter)
      and (garden_area_min_filter  is null or l.garden_area   >= garden_area_min_filter)
      and (garden_area_max_filter  is null or l.garden_area   <= garden_area_max_filter)
      and (bbox_west  is null or l.lng >= bbox_west)
      and (bbox_east  is null or l.lng <= bbox_east)
      and (bbox_south is null or l.lat >= bbox_south)
      and (bbox_north is null or l.lat <= bbox_north)
      and (building_condition_level_min  is null or l.building_condition_level  >= building_condition_level_min)
      and (building_condition_level_max  is null or l.building_condition_level  <= building_condition_level_max)
      and (apartment_condition_level_min is null or l.apartment_condition_level >= apartment_condition_level_min)
      and (apartment_condition_level_max is null or l.apartment_condition_level <= apartment_condition_level_max)
      and (price_change_count_min is null or
           (case when price_change_window_days = 30  then l.price_change_count_30d
                 when price_change_window_days = 90  then l.price_change_count_90d
                 when price_change_window_days = 365 then l.price_change_count_365d
                 else l.price_change_count end) >= price_change_count_min)
      and (total_price_change_pct_filter is null or total_price_change_pct_filter = 0
           or (total_price_change_pct_filter < 0 and l.total_price_change_pct <= total_price_change_pct_filter)
           or (total_price_change_pct_filter > 0 and l.total_price_change_pct >= total_price_change_pct_filter))
      and (not coalesce(with_estimates, false) or exists (
            select 1 from property_estimates_public pe where pe.property_id = l.property_id))
      and (obec_ids_filter is null or l.obec_id = any(obec_ids_filter))
      and (property_ids_filter is null or l.property_id = any(property_ids_filter))
      and (not hide_dismissed or not exists (
            select 1 from property_dismissals_public d where d.property_id = l.property_id))
      -- The third id space (see the parameter's comment). browse_list's twin has
      -- no equivalent; the map's does, and must.
      and (listing_ids_filter is null or l.listing_id = any(listing_ids_filter))
      and (tag_ids is null or array_length(tag_ids, 1) is null or l.property_id in (
          select pt.property_id from property_tags pt where pt.tag_id = any(tag_ids)
          group by pt.property_id having count(distinct pt.tag_id) = array_length(tag_ids, 1)))
      and (city_pop_min is null or l.home_obec_pop >= city_pop_min)
      and (city_pop_max is null or l.home_obec_pop <= city_pop_max)
      and (near_pop_5km_min      is null or l.near_pop_5km      >= near_pop_5km_min)
      and (near_pop_15km_min     is null or l.near_pop_15km     >= near_pop_15km_min)
      and (near_jobs_5km_min     is null or l.near_jobs_5km     >= near_jobs_5km_min)
      and (near_jobs_15km_min    is null or l.near_jobs_15km    >= near_jobs_15km_min)
      and (near_youth_5km_min    is null or l.near_youth_5km    >= near_youth_5km_min)
      and (near_youth_15km_min   is null or l.near_youth_15km   >= near_youth_15km_min)
      and (near_overall_5km_min  is null or l.near_overall_5km  >= near_overall_5km_min)
      and (near_overall_15km_min is null or l.near_overall_15km >= near_overall_15km_min)
      and (city_index_rules is null or jsonb_array_length(city_index_rules) = 0
           or l.obec_id = any (array(select curated_cities_matching(city_index_rules))))
      and true
    group by 1, 2
  )
  select jsonb_build_object(
    'total',        coalesce((select sum(n) from g), 0),
    'off_grid',     coalesce((select sum(n) from g where cx is null), 0),
    'cell_lat_deg', v_ch,
    'cell_lng_deg', v_cw,
    'grid_west',  v_w, 'grid_east',  v_e,
    'grid_south', v_s, 'grid_north', v_n,
    'cells', coalesce(
      (select jsonb_agg(jsonb_build_object('lat', la, 'lng', lo, 'n', n) order by n desc)
         from g where cx is not null),
      '[]'::jsonb)
  )
  into v_result;

  v_total := (v_result->>'total')::bigint;
  -- At or below the budget the caller re-reads the cohort as points, so the
  -- cells are withheld rather than shipped-and-ignored. `clustered` is what the
  -- caller branches on; it is never inferred from the array being empty (an
  -- empty cohort is not a small one).
  return v_result || jsonb_build_object(
    'clustered', v_total > point_budget,
    'cells', case when v_total > point_budget then v_result->'cells' else 'null'::jsonb end
  );
end
$function$;

commit;

-- ---------------------------------------------------------------------------
-- 5. Post-conditions, the locks still held. Shape everywhere; data on production only.
-- ---------------------------------------------------------------------------
do $post$
declare
  rel text;
  last_col text;
  populated bool;
  started timestamptz;
  n_checked bigint;
  n_wrong bigint;
  n_idx int;
begin
  foreach rel in array array['browse_projection', 'browse_list', 'properties_map_mv'] loop
    select a.attname into last_col from pg_attribute a
     where a.attrelid = ('public.' || rel)::regclass and a.attnum > 0 and not a.attisdropped
     order by a.attnum desc limit 1;
    if last_col is distinct from 'newest_ad_at_realitymix' then
      raise exception '590 did not land: the last column of % is %', rel, last_col;
    end if;
    if exists (select 1 from pg_attribute where attrelid = ('public.' || rel)::regclass
                and attname = 'asset_id' and not attisdropped) then
      raise exception '590 did not land: % still carries asset_id', rel;
    end if;
  end loop;
  if exists (select 1 from pg_attribute where attrelid = 'public.properties_public'::regclass
              and attname in ('asset_id', 'distinct_site_count', 'published_at') and not attisdropped)
     or not exists (select 1 from pg_attribute where attrelid = 'public.properties_public'::regclass
                     and attname = 'all_sources' and not attisdropped) then
    raise exception '590 did not land: properties_public has the wrong columns';
  end if;
  if to_regclass('public.browse_projection_legacy') is not null then
    raise exception '590 did not land: browse_projection_legacy still exists';
  end if;
  select count(*) into n_idx from pg_indexes
   where schemaname = 'public' and tablename = 'browse_list' and indexname like 'browse_list_newest_ad_at_%';
  if n_idx <> 9 then
    raise exception '590 did not land: browse_list has % per-portal indexes, not 9', n_idx;
  end if;
  if (select md5(prosrc) from pg_proc where oid = to_regprocedure('public.properties_map_visible()'))
     is distinct from '87ac5ed4f4c1eaaef58a06c9a4e42071'
     or (select md5(prosrc) from pg_proc where oid = to_regprocedure('public.rebuild_browse_list()'))
     is distinct from '8af37dfbacfecab1a73353bf36d24fba' then
    raise exception '590 did not land: a source or the rebuild is not this file''s body';
  end if;
  perform * from public.browse_list_visible() limit 1;
  perform * from public.properties_map_visible() limit 1;
  perform * from public.pipeline_board_public limit 1;

  select count(*) = 100000 into populated
    from (select 1 from public.properties limit 100000) probe;
  if not populated then
    raise notice '590: replay container, data post-conditions skipped';
    return;
  end if;
  if (select string_agg(md5(p.prosrc), ',') from pg_proc p
       where p.pronamespace = 'public'::regnamespace and p.proname = 'browse_stats_properties')
     is distinct from 'f4d95769ccc20ab9968d0135b86cac26'
     or (select string_agg(md5(p.prosrc), ',') from pg_proc p
          where p.pronamespace = 'public'::regnamespace and p.proname = 'browse_map_cells')
     is distinct from '0a1cf3a94fce99941adca7cbe1f375b9' then
    raise exception '590 did not land: the aggregate RPCs are not this file''s bodies';
  end if;

  -- The fill, row by row on a settled window: a browse_list row equals its property when the
  -- property was last recomputed well before the list rebuild began (its own start stamp).
  select last_succeeded_at into started from public.derived_artifacts where name = 'browse_list';
  select count(*),
         count(*) filter (where b.all_sources is distinct from coalesce(p.all_sources, '{}'::text[])
                             or b.active_sources is distinct from coalesce(p.active_sources, '{}'::text[])
                             or b.source_count is distinct from p.source_count
                             or (b.newest_ad_at_sreality, b.newest_ad_at_bazos, b.newest_ad_at_idnes,
                                 b.newest_ad_at_maxima, b.newest_ad_at_ceskereality,
                                 b.newest_ad_at_bezrealitky, b.newest_ad_at_mmreality,
                                 b.newest_ad_at_remax, b.newest_ad_at_realitymix)
                                is distinct from
                                (p.newest_ad_at_sreality, p.newest_ad_at_bazos, p.newest_ad_at_idnes,
                                 p.newest_ad_at_maxima, p.newest_ad_at_ceskereality,
                                 p.newest_ad_at_bezrealitky, p.newest_ad_at_mmreality,
                                 p.newest_ad_at_remax, p.newest_ad_at_realitymix))
    into n_checked, n_wrong
    from public.browse_list b
    join public.properties p on p.id = b.property_id
   where b.property_id >= 600000 and b.property_id < 610000
     and p.stats_computed_at < started - interval '15 minutes';
  if n_checked = 0 or n_wrong > 0 then
    raise exception '590 did not land: % of % settled browse_list rows disagree with properties', n_wrong, n_checked;
  end if;
  raise notice '590: % settled browse_list rows checked, 0 disagree (list rebuild began %)', n_checked, started;
end
$post$;

-- ---------------------------------------------------------------------------
-- 6. Hand the locks back; PostgREST re-reads the changed relations and functions.
-- ---------------------------------------------------------------------------
select pg_advisory_unlock(hashtext('rebuild_browse_list'));
select pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));

select pg_notify('pgrst', 'reload schema');

reset statement_timeout;
reset lock_timeout;
