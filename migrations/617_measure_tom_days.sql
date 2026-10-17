-- 617_measure_tom_days.sql
--
-- ONE DEFINITION OF TIME ON MARKET. W2 of the TOM sprint (W1 = PR #1849: the Browse list
-- finally applies the bound).
--
-- THE DEFECT: "days on market" was spelled out in full in four views (properties_public,
-- listings_public, listing_feed_public, browse_projection), once more in Python-emitted SQL
-- (toolkit/filter_compiler), once in toolkit/velocity, and once, DIFFERENTLY, in
-- toolkit/snapshots (it kept counting after a listing was delisted, so a closed ad aged
-- forever and fed outliers' `long_time_on_market`). Six spellings of one number is how W1's
-- bug lived unnoticed: a filter and the column it should read were maintained apart.
--
-- THE FIX is the shape of 425 (price per m2) and 534 (plot area): the measure is a function,
-- the views call it, the Python SQL emitters call it, nobody re-spells it.
--
--   measure_tom_days(is_active, first_seen_at, last_seen_at) -> integer days, floor, >= 0
--     active   now() - first_seen_at         (age so far; the run is still open)
--     inactive last_seen_at - first_seen_at  (the closed run's length)
--   Anchored on OUR first sighting: no portal publishes a usable "listed on" date (5 of 9
--   carry none, sreality's is the last EDIT, ceskereality's lags 26 days at the median), so a
--   listing already on the market when a portal was first walked reads younger than it is.
--
-- STABLE (it reads now()), PARALLEL SAFE, a single SELECT with NO `SET` clause: that is what
-- lets the planner inline it, so the views cost exactly what the inline CASE did and the
-- rebuilds of browse_list / properties_map_mv are unchanged. The numbers are identical too:
-- the body is the CASE the views carried. Step 3 PROVES the views changed in nothing else
-- (it re-derives each stored definition from its pre-image).
--
-- ORDER (the 584 recipe, minus the column work: no column is added, so no rebuild is forced):
--   1. both rebuild advisory locks first, queued behind an in-flight rebuild; from then on
--      every pg_cron tick self-skips in milliseconds and the view swap is uncontended.
--   2. one transaction: the function, the four views, the post-condition.
--   3. unlock; PostgREST re-reads the schema.
-- Re-runnable: create-or-replace throughout, and the post-condition holds on a second run.
-- ROLLBACK: restore the four views' prior bodies (590 / 517 / 584 / 590); the function can stay.

set statement_timeout = '1900s';
set lock_timeout = 0;

select pg_advisory_lock(hashtext('rebuild_browse_list'));
select pg_advisory_lock(hashtext('rebuild_properties_map_mv'));

set lock_timeout = '5s';
set statement_timeout = '900s';

begin;

-- ---------------------------------------------------------------------------
-- 1. THE MEASURE.
-- ---------------------------------------------------------------------------
create or replace function public.measure_tom_days(
  p_is_active boolean,
  p_first_seen_at timestamptz,
  p_last_seen_at timestamptz
)
 returns integer
 language sql
 stable parallel safe
as $function$
    select case
        when p_is_active then greatest(0, floor(extract(epoch from now() - p_first_seen_at) / 86400::numeric)::integer)
        else greatest(0, floor(extract(epoch from p_last_seen_at - p_first_seen_at) / 86400::numeric)::integer)
    end
$function$;

comment on function public.measure_tom_days(boolean, timestamptz, timestamptz) is
  'THE time-on-market measure, in whole days (floor, never negative). An ACTIVE row is its '
  'age so far, now() - first_seen_at: the run is still open, so the figure is a lower bound '
  'on its final length (right-censored). An INACTIVE row is the length of its closed run, '
  'last_seen_at - first_seen_at. NULL first_seen_at yields NULL. first_seen_at is OUR first '
  'sighting, not the portal''s listing date, so a listing already live when its portal was '
  'first walked reads younger than it is (left-censored). Mixing active and inactive rows in '
  'one cohort therefore mixes ages-so-far with completed durations. Python face: '
  'toolkit.measures.tom_days_sql. No SET search_path: a SET clause blocks inlining.';

revoke all on function public.measure_tom_days(boolean, timestamptz, timestamptz)
  from public, anon, authenticated;
grant execute on function public.measure_tom_days(boolean, timestamptz, timestamptz)
  to authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 2. THE FOUR VIEWS, each body verbatim from its latest definer with only the CASE replaced:
--    properties_public (590), listings_public (517), listing_feed_public (584),
--    browse_projection (590). Same columns, same order, same types -- so browse_list,
--    properties_map_mv and the three functions typed on browse_projection's row are untouched.
-- ---------------------------------------------------------------------------
-- The pre-images ride in a transaction-local setting, not a temp table: the receipt parser
-- would read a top-level CREATE TEMP TABLE as an object this migration must leave behind.
select set_config('tom617.pre', (
  select jsonb_object_agg(c.relname, regexp_replace(pg_get_viewdef(c.oid), '\s+', ' ', 'g'))::text
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public'
    and c.relname in ('properties_public', 'listings_public', 'listing_feed_public', 'browse_projection')
), true);

-- properties_public
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
      measure_tom_days(p.is_active, p.first_seen_at, p.last_seen_at) as tom_days,
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
-- listings_public
create or replace view listings_public as
select
  sreality_id,
  first_seen_at,
  last_seen_at,
  is_active,
  category_main,
  category_type,
  price_czk,
  price_unit,
  area_m2,
  disposition,
  st_y(ll.geom) as lat,
  st_x(ll.geom) as lng,
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
  measure_tom_days(is_active, first_seen_at, last_seen_at) as tom_days,
  measure_price_per_m2(price_czk::numeric, area_m2::numeric, category_main, category_type) as price_per_m2,
  description,
  source,
  mf_reference_rent_czk,
  mf_gross_yield_pct,
  mf_reference_rent,
  subtype,
  id,
  source_id_native,
  property_id,
  measure_price_per_m2_basis(category_main, category_type) as price_per_m2_basis,
  source_url,
  location_display_label(ll.street_name, ll.house_number_cp, ll.house_number_co,
                         ll.obec_name, ll.cast_obce_name, ll.country_code,
                         ll.country_status) as display_label
from listings
     left join listing_location ll on ll.listing_id = listings.id;
-- listing_feed_public
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
  measure_tom_days(l.is_active, l.first_seen_at, l.last_seen_at) as tom_days,
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
                      l.estate_area::numeric) as plot_area_m2,
  ll.ulice_kod as ulice_id
from listings l
join properties p on p.id = l.property_id
left join listing_location ll on ll.listing_id = l.id
left join location_granularity_rank gr on gr.granularity = ll.granularity
left join lateral public.browse_list_mf(l.property_id) rm on true
where p.status = 'active'
  -- THE CONSUMER RULE == location_data.claims_common.SERVED_LOCATION_PREDICATE,
  --    rendered on the FEED's own listing. Pinned by tests/test_location_w5_serve_resolved.py.
  and EXISTS (SELECT 1 FROM listing_location sl WHERE sl.listing_id = l.id AND (sl.geom IS NOT NULL OR sl.country_status = 'foreign'));
-- browse_projection
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
      measure_tom_days(p.is_active, p.first_seen_at, p.last_seen_at) as tom_days,
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

-- ---------------------------------------------------------------------------
-- 3. POST-CONDITION: each view now equals its pre-image with the inline CASE swapped for the
--    call, and NOTHING else changed -- a repo-vs-live drift in any other column or join would
--    abort the transaction here, before COMMIT.
-- ---------------------------------------------------------------------------
do $post$
declare
  r record;
  want text;
  got text;
begin
  for r in select key as relname, value as def from jsonb_each_text(current_setting('tom617.pre')::jsonb) order by key loop
    want := regexp_replace(
      r.def,
      'CASE WHEN (\w+)\.is_active THEN GREATEST\(0, \(floor\(\(EXTRACT\(epoch FROM \(now\(\) - \1\.first_seen_at\)\) / \(86400\)::numeric\)\)\)::integer\) '
      'ELSE GREATEST\(0, \(floor\(\(EXTRACT\(epoch FROM \(\1\.last_seen_at - \1\.first_seen_at\)\) / \(86400\)::numeric\)\)\)::integer\) END',
      'measure_tom_days(\1.is_active, \1.first_seen_at, \1.last_seen_at)');
    select regexp_replace(pg_get_viewdef(c.oid), '\s+', ' ', 'g') into got
      from pg_class c join pg_namespace n on n.oid = c.relnamespace
      where n.nspname = 'public' and c.relname = r.relname;
    if got is distinct from want then
      raise exception '617: % changed in more than its tom_days expression', r.relname;
    end if;
    if got like '%86400%' then
      raise exception '617: % still spells time on market inline', r.relname;
    end if;
  end loop;
end
$post$;

commit;

select pg_advisory_unlock(hashtext('rebuild_browse_list'));
select pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));

select pg_notify('pgrst', 'reload schema');

reset statement_timeout;
reset lock_timeout;
