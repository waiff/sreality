-- 508_location_w4c_legacy_drops.sql
-- W4-c -- the legacy location columns, caches and mirrors are DROPPED.
--
-- ===========================================================================
-- DESTRUCTIVE. Do not apply without the operator's explicit word and a
-- pg_dump first (architecture rule 1).
--
--   pg_dump "$SUPABASE_DB_URL" --no-owner --no-acl \
--     -t public.listings -t public.properties \
--     -t public.geocode_cache \
--     -t public.mapy_affected -t public.mapy_affected_cache \
--     -t public.mapy_affected_props -t public.mapy_inventory_runs \
--     -t public.address_points -t public.address_points_revisions \
--     -f w4c-pre-drop-$(date -u +%Y%m%dT%H%M%SZ).sql
--
-- `listings` and `properties` are dumped WHOLE: a column-scoped dump is not a
-- thing, and the 24 + 15 columns this file removes are only recoverable from a
-- full table dump. ~390k active listings / ~635k properties.
--
-- APPLY WINDOW: 05:20-05:30 UTC. Between the */15 `rebuild_browse_list` ticks
-- and off the :30/:40/:45/:50 detail-drain crons -- the window migration 429
-- chose, for the same reason: `alter table listings drop column` takes ACCESS
-- EXCLUSIVE on the hottest table in the database. The ALTER itself is
-- catalog-only (Postgres marks the attribute dropped and rewrites nothing), so
-- it is fast ONCE THE LOCK IS GRANTED; the whole risk is queueing behind a
-- reader. Hence one ALTER with 24 DROP COLUMN clauses (one lock acquisition,
-- not 24) and `lock_timeout = '5s'` -- do NOT raise it, a long timeout queues
-- and blocks every write behind it. `apply_migration.yml`'s 30 x 20 s retry
-- loop does the waiting.
--
-- RUN ORDER -- DEPLOY THE CODE FIRST, THEN APPLY THIS FILE.
-- The reverse of every other migration in this sprint. The bundle running
-- today WRITES these columns on every scrape and READS them in a dozen
-- queries; applying first would break the ingest within one drain tick. The
-- PR's code half stops both, so:
--   1. merge the PR -> Railway rolls out API + realtime-worker + SPA,
--   2. confirm the rollout (gh api .../commits/<sha>/status),
--   3. pg_dump,
--   4. apply this file in the window above.
-- Between (1) and (4) the columns simply sit there unread. There is no window
-- in which live code needs a column this file has already dropped.
--
-- APPLY PATH: `apply_migration.yml` (`psql -X -q -v ON_ERROR_STOP=1 -f`), NOT
-- the Supabase MCP. Statements autocommit -- there is NO begin/commit in this
-- file and there must not be: a partial apply must be resumable by re-running
-- the file from statement 1, which is why every statement is `if exists` /
-- drop-then-create. The `set` below is therefore a plain SET, never SET LOCAL
-- (outside a transaction SET LOCAL is a silent no-op).
-- ===========================================================================
--
-- WHAT GOES
--
--   listings  (24 columns): geom, obec_id, okres_id, region_id, ku_id,
--     locality, district, obec, okres, region, street, house_number, zip,
--     street_id, locality_municipality_id, locality_quarter_id,
--     locality_ward_id, locality_district_id, locality_region_id,
--     street_name_key, street_source, geo_cell_key,
--     coord_street_attempt_version, geocode_attempted_at
--   properties (15 columns): geom, lat, lng, district, locality, street, obec,
--     okres, region, obec_id, okres_id, region_id, locality_district_id,
--     locality_region_id, ku_id  (place_search_text / home_city_id went in 506)
--   tables: geocode_cache, mapy_affected, mapy_affected_cache,
--     mapy_affected_props, mapy_inventory_runs, address_points,
--     address_points_revisions
--   triggers + functions: trg_listings_geo_cell_key, trg_listings_admin_geo,
--     properties_set_latlng_trg and their four functions;
--     mapy_inventory_immutable(); region_stats(), region_active_by_day(),
--     browse_stats() -- three unreferenced RPCs whose last live reader was a
--     column this file drops (425's own header records browse_stats's
--     "intended end state is DROP, held back only for want of operator
--     sign-off on a destructive step"; this is that sign-off).
--
-- WHAT STAYS, and why
--
--   admin_boundaries -- price stats, the rent map and recompute_city_proximity
--     still read its geometry and population. Its LOCATION role (trigger 289's
--     PIP) dies here; the price-stats/rent-map re-key onto
--     ruian_admin_unit_geometries is a later wave.
--   curated_cities.admin_boundary_id -- an FK to that table, and already the
--     RUIAN obec code.
--   portal_raw_pages -- preservation substrate, 447k rows / 14 GB, 12 live
--     writers and a live scrape-path read; tests/test_portal_raw_pages_guard.py
--     fails CI on any DROP naming it. Not a location path.
--   location_granularity_rank -- the 11-row rank lookup every granularity
--     comparison goes through.
--   properties.home_obec_pop / near_*_{5,15}km -- Browse filters read them.
--     Their producer, recompute_city_proximity(), is re-sourced below onto
--     listing_location rather than dropped.
--   raw_json -- untouched. It is the content-hash substrate (rule 2) and the
--     resolver's evidence; the legacy keys stay in it as history forever.

set lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 1. The views that still PROJECT a column being dropped.
--
--    `create or replace view` can only append; removing an output column needs
--    DROP + CREATE, and a dropped view loses its ACL -- so every revoke/grant
--    is re-stated verbatim for BOTH browser roles below.
--
--    Dependency order matters twice over (each statement autocommits):
--      pipeline_board_public   -> properties_public
--      properties_map_mv       -> browse_projection
--      image_storage_overview_mv / scraper_health_checks_mv / health_summary_mv
--                              -> listings_public
--      broker_geo_options      -> broker_region_type_stats
--    so the dependents drop first and are re-created after.
--
--    The three health matviews are the surprise in this section: none of them
--    reads a dropped COLUMN (they take source / sreality_id / category_* /
--    is_active / first_seen_at / last_seen_at off `listings_public`), but a
--    matview holds an object-level dependency on the view, so `drop view
--    listings_public` is refused while they exist. Their bodies below are
--    migration 354's, carried over VERBATIM -- not retyped, not re-planned --
--    together with their unique indexes and revokes. `create materialized
--    view` populates, which is why statement_timeout is raised around them:
--    their pg_cron refresh (*/10, migration 371) uses REFRESH ... CONCURRENTLY
--    and would fail on a never-populated matview.
--
--    `rebuild_properties_map_mv()` is NOT re-created: 506 already took
--    `district` out of its cover INCLUDE list, and nothing else in its body
--    names a dropped column. `properties_map_mv` is simply dropped here and
--    rebuilt by that function in section 6.
--
--    `data_quality_by_source` keeps its five OUTPUT columns, so it takes a
--    plain `create or replace` (and keeps its ACL and its platform-admin
--    gate). What changes is inside: seven legacy field probes become three
--    read off `listing_location`. `geom` and `locality` keep their NAMES on
--    purpose -- scraper_health_checks_mv watches exactly
--    price_czk/area_m2/geom/locality/disposition against the daily
--    data_quality_snapshots baseline, and renaming them would silently drop
--    two of the five field-population alarms.
-- ---------------------------------------------------------------------------

drop view if exists pipeline_board_public;
drop materialized view if exists properties_map_mv;
drop view if exists properties_public;
drop view if exists browse_projection;
drop view if exists listing_feed_public;
drop materialized view if exists image_storage_overview_mv;
drop materialized view if exists scraper_health_checks_mv;
drop materialized view if exists health_summary_mv;
drop view if exists listings_public;
drop view if exists broker_geo_options;
drop materialized view if exists broker_region_type_stats;
drop view if exists broker_listings_public;

-- 1a. browse_projection -- 506's body, `locality_district_id` and
--     `locality_region_id` removed. They were sreality portal ids, never a
--     query dimension; W3 took the last filter that named them.
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
    -- RE-SOURCED (W3-2): the pin is the resolver's point, with a stated radius,
    -- or it is no pin at all.
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
    -- RE-SOURCED (W3-2): value-identical for a resolved row -- admin_boundaries.id
    -- IS the RÚIAN code, so these three keep matching the same chips.
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
    p.all_sources,
    p.active_sources,
    measure_price_per_m2_basis(p.category_main, p.category_type) as price_per_m2_basis,
    -- ---- appended by migration 503 (W3 S1) ----
    location_display_label(ll.street_name, ll.house_number_cp, ll.house_number_co,
                           ll.obec_name, ll.cast_obce_name, ll.country_code,
                           ll.country_status) as display_label,
    -- The level `listing_location` adds and no chip has today (S3 gives it one).
    ll.cast_obce_kod as cast_obce_id,
    -- The two the map's circle rule reads. `granularity_rank` is an INT from the
    -- 11-row lookup, never the enum's ordinality and never its text: the SPA
    -- compares numbers (`< BUILDING_RANK`), which is the only legal way to
    -- compare granularity (migration 380).
    ll.uncertainty_radius_m,
    gr.rank::int as granularity_rank
from properties p
     left join listing_location ll on ll.listing_id = p.repr_listing_ref_id
     left join location_granularity_rank gr on gr.granularity = ll.granularity
where p.status = 'active'::text;

revoke all on browse_projection from anon, authenticated;
grant select on browse_projection to authenticated;

-- 1b. listing_feed_public -- 506's body, the seven legacy place columns
--     removed. `display_label` (503) is the one place string this lane
--     renders, and the three chip codes it already takes from listing_location
--     are the only place values it filters on.
create view listing_feed_public as
select
  l.id,
  l.source,
  l.source_id_native,
  l.sreality_id,                          -- legacy compat only; NULL post-Gate-2 for non-sreality
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
  l.mf_gross_yield_pct,
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
  -- ---- appended by migration 503 (W3 S1) ----
  location_display_label(ll.street_name, ll.house_number_cp, ll.house_number_co,
                         ll.obec_name, ll.cast_obce_name, ll.country_code,
                         ll.country_status) as display_label,
  ll.cast_obce_kod as cast_obce_id,
  ll.uncertainty_radius_m,
  gr.rank::int as granularity_rank
from listings l
join properties p on p.id = l.property_id
left join listing_location ll on ll.listing_id = l.id
left join location_granularity_rank gr on gr.granularity = ll.granularity
where p.status = 'active';

revoke all on listing_feed_public from anon, authenticated;
grant select on listing_feed_public to authenticated;

-- 1c. listings_public -- 507's body, nine legacy place columns removed. The
--     three chip codes, lat/lng and display_label already come from
--     listing_location; nothing in api/, toolkit/, frontend/src/ or the
--     extension selects from this view (migration 494 censused it), and its
--     only readers are the three health matviews re-created in 1d.
create view listings_public as
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
  broker_name,
  null::text as broker_email,
  null::text as broker_phone,
  case
    when is_active then greatest(0, floor(extract(epoch from now() - first_seen_at) / 86400::numeric)::integer)
    else greatest(0, floor(extract(epoch from last_seen_at - first_seen_at) / 86400::numeric)::integer)
  end as tom_days,
  measure_price_per_m2(price_czk::numeric, area_m2::numeric, category_main, category_type) as price_per_m2,
  building_condition_level,
  apartment_condition_level,
  description,
  source,
  mf_reference_rent_czk,
  mf_gross_yield_pct,
  mf_reference_rent,
  subtype,
  ll.obec_kod  as obec_id,
  ll.okres_kod as okres_id,
  ll.kraj_kod  as region_id,
  id,
  source_id_native,
  property_id,
  measure_price_per_m2_basis(category_main, category_type) as price_per_m2_basis,
  source_url,
  -- ---- appended by migration 503 (W3 S1) ----
  location_display_label(ll.street_name, ll.house_number_cp, ll.house_number_co,
                         ll.obec_name, ll.cast_obce_name, ll.country_code,
                         ll.country_status) as display_label
from listings
     left join listing_location ll on ll.listing_id = listings.id;

revoke all on listings_public from anon, authenticated;
grant select on listings_public to authenticated;

-- 1d. The three health matviews, VERBATIM from migration 354. They lose
--     nothing -- they are here only because they hold a dependency on
--     listings_public.
set statement_timeout = '900s';

create materialized view image_storage_overview_mv as
  select
    l.category_main,
    l.category_type,
    count(i.id)                                       as total,
    count(i.storage_path)                             as stored,
    count(i.id) filter (where l.is_active)            as total_active,
    count(i.storage_path) filter (where l.is_active)  as stored_active
  from listings_public l
  left join images i on i.listing_id = l.id
  group by 1, 2;

create unique index if not exists image_storage_overview_mv_cat
  on image_storage_overview_mv (category_main, category_type);

revoke all on image_storage_overview_mv from anon, authenticated;

create materialized view scraper_health_checks_mv as
with
sources as (
  select source, coalesce(scrape_cadence_minutes, 60) as cad_mins
  from portals
  where kind = 'scraper'
),
runs_agg as (
  select
    source,
    max(started_at) filter (where index_pages > 0) as last_start,
    count(*) filter (where ended_at is null
                       and started_at < now() - interval '30 minutes'
                       and started_at > now() - interval '6 hours') as stuck,
    coalesce(sum(listings_scraped_new) filter (where started_at > now() - interval '24 hours'), 0) as scraped_new,
    coalesce(sum(listings_updated)     filter (where started_at > now() - interval '24 hours'), 0) as updated,
    coalesce(max(listings_inactive)    filter (where started_at > now() - interval '24 hours'), 0) as inactive_max,
    coalesce(sum(errors)               filter (where started_at > now() - interval '24 hours'), 0) as errors_sum
  from scrape_runs_public
  group by source
),
listings_agg as (
  select
    source,
    count(*) filter (where first_seen_at > now() - interval '24 hours') as new_listings_fs,
    count(*) filter (where is_active and last_seen_at < now() - interval '7 days') as stale_active,
    max(last_seen_at) filter (where is_active) as last_fresh
  from listings_public
  group by source
),
fails_agg as (
  select
    coalesce(l.source, 'sreality') as source,
    count(*) filter (where not f.given_up) as active_fail,
    count(*) filter (where f.given_up) as given_up
  from listing_fetch_failures_public f
  left join listings_public l on l.sreality_id = f.sreality_id
  group by coalesce(l.source, 'sreality')
),
queue_agg as (
  select
    source,
    count(*) filter (where claimed_at is null and not given_up) as claimable,
    count(*) filter (where claimed_at is null and not given_up and priority = 1) as changed,
    count(*) filter (where given_up) as q_given_up
  from listing_detail_queue_public
  group by source
),
lag_agg as (
  select
    q.source,
    coalesce(round((percentile_cont(0.5) within group (order by extract(epoch from now() - q.enqueued_at)/60.0))::numeric, 1), 0) as p50_min,
    coalesce(round((percentile_cont(0.9) within group (order by extract(epoch from now() - q.enqueued_at)/60.0))::numeric, 1), 0) as p90_min,
    count(*) filter (where q.enqueued_at < now() - make_interval(mins => (s.cad_mins * 3)::int))::int as unhealthy_n,
    count(*)::int as n
  from listing_detail_queue_public q
  join sources s on s.source = q.source
  where q.claimed_at is null and not q.given_up and q.priority <> 2
  group by q.source
),
attach_agg as (
  select
    source,
    count(*)::int as n,
    coalesce(round(extract(epoch from now() - min(first_seen_at))/60.0, 1), 0) as oldest_min
  from listings
  where is_active and property_id is null
  group by source
),
delist_agg as (
  select
    source,
    count(*)::int as n,
    coalesce(round((percentile_cont(0.5) within group (order by extract(epoch from inactive_at - last_seen_at)/60.0))::numeric, 1), 0) as p50_min,
    coalesce(round((percentile_cont(0.9) within group (order by extract(epoch from inactive_at - last_seen_at)/60.0))::numeric, 1), 0) as p90_min
  from listings
  where inactive_at is not null
    and inactive_at > now() - interval '7 days'
  group by source
),
churn_agg as (
  select source, snaps_24h, active_n,
         round(snaps_24h / nullif(active_n, 0)::numeric, 2) as ratio
  from snapshot_churn_24h_mv
),
drift_fresh as (
  select distinct on (source, field) source, field, pct_populated
  from data_quality_snapshots
  where field in ('price_czk', 'area_m2', 'geom', 'locality', 'disposition')
    and captured_at > now() - interval '20 hours'
  order by source, field, captured_at desc
),
drift_baseline as (
  select distinct on (source, field) source, field, pct_populated
  from data_quality_snapshots
  where field in ('price_czk', 'area_m2', 'geom', 'locality', 'disposition')
    and captured_at < now() - interval '20 hours'
    and captured_at > now() - interval '8 days'
  order by source, field, captured_at desc
),
drift_agg as (
  select
    f.source,
    count(*)::int as n_fields,
    coalesce(max(b.pct_populated - f.pct_populated), 0) as max_drift,
    (array_agg(f.field order by (b.pct_populated - f.pct_populated) desc))[1] as worst_field
  from drift_fresh f
  join drift_baseline b using (source, field)
  group by f.source
),
recon_agg as (
  select
    s.source,
    count(d.gap_pct) as n_with_data,
    max(d.gap_pct) as max_gap_pct
  from sources s
  left join lateral (
    select by_category
    from scrape_runs_public
    where ended_at is not null and index_pages > 0 and source = s.source
    order by started_at desc
    limit 1
  ) latest on true
  left join lateral (
    select abs((e->>'collected')::numeric - (e->>'sreality_result_size')::numeric)
             / nullif((e->>'sreality_result_size')::numeric, 0) * 100.0 as gap_pct
    from jsonb_array_elements(coalesce(latest.by_category, '[]'::jsonb)) e
    where (e->>'sreality_result_size') is not null and (e->>'collected') is not null
      and (e->>'sreality_result_size')::numeric > 0
  ) d on true
  group by s.source
),
calc as (
  select
    s.source,
    s.cad_mins,
    ra.last_start,
    extract(epoch from now() - ra.last_start)/60.0 as mins_since_start,
    coalesce(ra.stuck, 0)        as stuck,
    coalesce(ra.scraped_new, 0)  as scraped_new,
    coalesce(ra.updated, 0)      as updated,
    coalesce(ra.inactive_max, 0) as inactive_max,
    coalesce(ra.errors_sum, 0)   as errors_sum,
    round(100.0 * coalesce(ra.errors_sum, 0)
          / nullif(coalesce(ra.errors_sum, 0) + coalesce(ra.scraped_new, 0) + coalesce(ra.updated, 0), 0), 1) as err_pct,
    coalesce(la.new_listings_fs, 0) as new_listings_fs,
    coalesce(la.stale_active, 0)    as stale_active,
    extract(epoch from now() - la.last_fresh)/60.0 as mins_fresh,
    coalesce(fa.active_fail, 0) as active_fail,
    coalesce(fa.given_up, 0)    as given_up,
    coalesce(qa.claimable, 0)   as q_claimable,
    coalesce(qa.changed, 0)     as q_changed,
    coalesce(qa.q_given_up, 0)  as q_given_up,
    coalesce(lg.p50_min, 0)     as lag_p50,
    coalesce(lg.p90_min, 0)     as lag_p90,
    coalesce(lg.unhealthy_n, 0) as lag_unhealthy,
    coalesce(lg.n, 0)           as lag_n,
    coalesce(at.n, 0)           as attach_n,
    coalesce(at.oldest_min, 0)  as attach_oldest,
    coalesce(dl.n, 0)           as delist_n,
    coalesce(dl.p50_min, 0)     as delist_p50,
    coalesce(dl.p90_min, 0)     as delist_p90,
    coalesce(ch.snaps_24h, 0)   as churn_snaps,
    coalesce(ch.active_n, 0)    as churn_active,
    coalesce(ch.ratio, 0)       as churn_ratio,
    coalesce(dr.n_fields, 0)    as drift_nfields,
    coalesce(dr.max_drift, 0)   as drift_max,
    dr.worst_field             as drift_worst,
    coalesce(rc.n_with_data, 0) as recon_n,
    rc.max_gap_pct             as recon_gap
  from sources s
  left join runs_agg     ra on ra.source = s.source
  left join listings_agg la on la.source = s.source
  left join fails_agg    fa on fa.source = s.source
  left join queue_agg    qa on qa.source = s.source
  left join lag_agg      lg on lg.source = s.source
  left join attach_agg   at on at.source = s.source
  left join delist_agg   dl on dl.source = s.source
  left join churn_agg    ch on ch.source = s.source
  left join drift_agg    dr on dr.source = s.source
  left join recon_agg    rc on rc.source = s.source
)
select
  c.source,
  jsonb_build_object(
    'source', c.source,
    'checks', jsonb_build_array(
      jsonb_build_object(
        'key', 'liveness', 'label', 'Scraper running on schedule',
        'status', case when c.last_start is null then 'warn'
                       when c.mins_since_start < c.cad_mins * 1.5 then 'pass'
                       when c.mins_since_start < c.cad_mins * 3 then 'warn' else 'fail' end,
        'value', case when c.last_start is null then 'never'
                      else coalesce(round(c.mins_since_start::numeric, 0)::text, '–') || ' min ago' end,
        'detail', 'Last index walk started ' || coalesce(to_char(c.last_start, 'YYYY-MM-DD HH24:MI'), 'never')
                  || ' UTC. Expected cadence ~' || c.cad_mins::text || ' min (GitHub throttles short crons). '
                  || 'Warn >' || round(c.cad_mins * 1.5)::text || ' min, fail >' || round(c.cad_mins * 3)::text || ' min.'),
      jsonb_build_object('key', 'runs_completing', 'label', 'Runs finishing cleanly',
        'status', case when c.stuck = 0 then 'pass' when c.stuck = 1 then 'warn' else 'fail' end,
        'value', c.stuck::text || ' stuck',
        'detail', 'Index-walk or detail-drain runs started >30 min ago (last 6h) that never recorded an end timestamp — a crash or timeout before finalize. Expected 0.'),
      jsonb_build_object('key', 'new_listings', 'label', 'New listings flowing',
        'status', case when c.new_listings_fs > 0 then 'pass' else 'warn' end,
        'value', c.new_listings_fs::text || ' / 24h',
        'detail', 'New listings first seen in the last 24h (from listings.first_seen_at — immune to a crashed or SIGKILLed drain''s lost run counters). 0 over a full day suggests the index-walk enqueue or the detail-drain is blocked.'),
      jsonb_build_object('key', 'delisting_spike', 'label', 'No false mass-delisting',
        'status', case when c.inactive_max <= 500 then 'pass' when c.inactive_max <= 2000 then 'warn' else 'fail' end,
        'value', c.inactive_max::text || ' max/run',
        'detail', 'Largest single-run inactivation in 24h (the index-walk''s mark_inactive). A big spike usually means a truncated index walk falsely delisted live listings; the walk-completeness guard mitigates this. Warn >500, fail >2000.'),
      jsonb_build_object('key', 'delisting_latency', 'label', 'Delisting latency (gone → flipped)',
        'status', case when c.delist_n = 0 then 'pass'
                       when c.delist_p90 < 2160 then 'pass'
                       when c.delist_p90 < 4320 then 'warn' else 'fail' end,
        'value', case when c.delist_n = 0 then 'no flips recorded yet'
                      else 'p50 ' || c.delist_p50::text || 'm / p90 ' || c.delist_p90::text || 'm' end,
        'detail', 'How long a delisted listing stayed nominally active: inactive_at − last_seen_at over the '
                  || c.delist_n::text || ' listings flipped inactive in the last 7 days. Rows flipped before migration 175 carry no stamp and are ignored. Warn p90 >36h (2160 min), fail >72h (4320 min).'),
      jsonb_build_object('key', 'error_rate', 'label', 'Detail-fetch error rate',
        'status', case when coalesce(c.err_pct, 0) < 5 then 'pass' when coalesce(c.err_pct, 0) < 15 then 'warn' else 'fail' end,
        'value', coalesce(c.err_pct, 0)::text || '%',
        'detail', 'Errors as a share of detail work (errors + new + updated) over 24h. Elevated values usually mean the portal is rate-limiting. Warn >5%, fail >15%.'),
      jsonb_build_object('key', 'snapshot_churn', 'label', 'Snapshot churn (hash thrash)',
        'status', case when coalesce(c.churn_ratio, 0) < 0.5 then 'pass'
                       when coalesce(c.churn_ratio, 0) < 1.5 then 'warn' else 'fail' end,
        'value', coalesce(c.churn_ratio, 0)::text || '× / 24h',
        'detail', c.churn_snaps::text || ' snapshots written in the last 24h across ' || c.churn_active::text
                  || ' active listings. A ratio near 1 means the average listing re-snapshots DAILY — almost always a volatile field thrashing the content hash (the idnes A/B/A storm ran for weeks undetected), not real market churn. Warn ≥0.5, fail ≥1.5.'),
      jsonb_build_object('key', 'stale_active', 'label', 'No stale active listings',
        'status', case when c.stale_active < 50 then 'pass' when c.stale_active < 500 then 'warn' else 'fail' end,
        'value', c.stale_active::text,
        'detail', 'Listings still is_active=true but not seen in the index for >7 days — they should have been marked inactive. Warn >50, fail >500.'),
      jsonb_build_object('key', 'field_null_drift', 'label', 'Field completeness drift',
        'status', case when c.drift_nfields = 0 then 'pass'
                       when c.drift_max < 5 then 'pass'
                       when c.drift_max < 15 then 'warn' else 'fail' end,
        'value', case when c.drift_nfields = 0 then 'no baseline yet'
                      else c.drift_worst || ' −' || round(greatest(c.drift_max, 0), 1)::text || ' pts' end,
        'detail', 'Largest drop in field population (percentage points) vs the daily data-quality baseline (data_quality_snapshots, latest capture 20h–8d old), across price_czk / area_m2 / geom / locality / disposition. Catches a parser silently losing a field within a day — the bazos locality breakage took weeks to surface this way. Warn ≥5 pts, fail ≥15 pts.'),
      jsonb_build_object('key', 'fetch_failures', 'label', 'Fetch-failure backlog',
        'status', case when c.active_fail < 1000 then 'pass' when c.active_fail < 5000 then 'warn' else 'fail' end,
        'value', c.active_fail::text || ' active',
        'detail', c.given_up::text || ' listings given up after repeated failures. Active failures retry with priority next run. Warn >1000, fail >5000.'),
      jsonb_build_object('key', 'detail_queue_backlog', 'label', 'Detail-drain backlog',
        'status', case when c.q_claimable < 2000 then 'pass' when c.q_claimable < 10000 then 'warn' else 'fail' end,
        'value', c.q_claimable::text || ' queued',
        'detail', 'New + price-changed listings the index walk enqueued but the detail-drain has not fetched yet ('
                  || c.q_changed::text || ' price-changed). A new listing becomes an active row only once drained, so THIS backlog — not data loss — is what opens the gap in "Index walk completeness". The drain closes it; raise its cap/cadence if it grows. '
                  || c.q_given_up::text || ' given up. Warn >2k, fail >10k.'),
      jsonb_build_object('key', 'detail_queue_lag', 'label', 'Detail-drain lag (index→fetch)',
        'status', case when c.lag_n = 0 then 'pass'
                       when c.lag_p90 < c.cad_mins * 1.5 then 'pass'
                       when c.lag_p90 < c.cad_mins * 3 then 'warn' else 'fail' end,
        'value', case when c.lag_n = 0 then 'empty'
                      else 'p50 ' || c.lag_p50::text || 'm / p90 ' || c.lag_p90::text || 'm' end,
        'detail', 'Time between the index walk enqueueing a listing and the detail-drain fetching it, over listings still waiting (in-flight only — completed queue rows are deleted, so a caught-up drain reads empty). '
                  || c.lag_unhealthy::text || ' have waited >' || round(c.cad_mins * 3)::text || ' min (~3 missed cycles). '
                  || 'Fresh + price-changed rows only (excludes failure-retry). Warn p90 >' || round(c.cad_mins * 1.5)::text || ' min, fail p90 >' || round(c.cad_mins * 3)::text || ' min.'),
      jsonb_build_object('key', 'property_attach_lag', 'label', 'Property attach lag (Browse-visible)',
        'status', case when c.attach_n = 0 then 'pass'
                       when c.attach_oldest < 30 then 'pass'
                       when c.attach_oldest < 90 then 'warn' else 'fail' end,
        'value', case when c.attach_n = 0 then 'all attached'
                      else c.attach_n::text || ' waiting, oldest ' || c.attach_oldest::text || 'm' end,
        'detail', 'A scraped listing lands with no properties row and is invisible in Browse (which reads the property grain) until the async property-maintenance job (recompute_property_stats --incremental, ~every 5 min; daily full sweep as backstop) attaches it as a singleton. The remaining gap between "scraped into listings" and "Browse-visible" — pairs with the detail-drain lag above for end-to-end latency. Warn oldest >30 min, fail >90 min.'),
      jsonb_build_object('key', 'e2e_latency', 'label', 'End-to-end latency (portal → Browse)',
        'status', case when (c.lag_p90 + c.attach_oldest) < 90 then 'pass'
                       when (c.lag_p90 + c.attach_oldest) < 240 then 'warn' else 'fail' end,
        'value', round((c.lag_p90 + c.attach_oldest)::numeric, 0)::text || ' min',
        'detail', 'Composed pipeline latency: detail-drain p90 (' || c.lag_p90::text
                  || 'm, index-seen → fetched) + oldest unattached listing (' || c.attach_oldest::text
                  || 'm, fetched → Browse-visible). The two segment checks above are the components; this is the single "how far behind the portal is Browse" number. Warn ≥90 min, fail ≥240 min.'),
      jsonb_build_object('key', 'data_freshness', 'label', 'Data freshness',
        'status', case when c.mins_fresh is null then 'warn'
                       when c.mins_fresh < c.cad_mins then 'pass'
                       when c.mins_fresh < c.cad_mins * 3 then 'warn' else 'fail' end,
        'value', case when c.mins_fresh is null then '–'
                      else coalesce(round(c.mins_fresh::numeric, 0)::text, '–') || ' min' end,
        'detail', 'Time since the most recently seen active listing. Warn >' || c.cad_mins::text || ' min, fail >' || round(c.cad_mins * 3)::text || ' min.'),
      jsonb_build_object('key', 'index_completeness', 'label', 'Index walk completeness',
        'status', case when c.recon_n = 0 then 'warn'
                    when coalesce(c.recon_gap, 0) < 2 then 'pass'
                    when coalesce(c.recon_gap, 0) < 5 then 'warn' else 'fail' end,
        'value', case when c.recon_n = 0 then 'no data yet'
                      else round(coalesce(c.recon_gap, 0), 1)::text || '% max gap' end,
        'detail', 'Largest per-category gap between how many index entries we collected and the portal''s reported result_size on the latest completed index walk — i.e. did the walk SEE every listing. Whether we have FETCHED them is the separate detail-drain backlog. Populates once the walk records per-category result_size. Warn >2%, fail >5%.')
    )
  ) as payload
from calc c;

create unique index if not exists scraper_health_checks_mv_source_idx
  on scraper_health_checks_mv (source);

revoke all on scraper_health_checks_mv from anon, authenticated;

create materialized view health_summary_mv as
with
category_pairs as (
  select * from (values
    ('byt',      'pronajem', 1),
    ('byt',      'prodej',   2),
    ('dum',      'pronajem', 3),
    ('dum',      'prodej',   4),
    ('komercni', 'pronajem', 5),
    ('komercni', 'prodej',   6)
  ) as t(category_main, category_type, sort_order)
),
series14 as (
  select generate_series((now() - interval '13 days')::date, now()::date, '1 day')::date as day
),
series7 as (
  select generate_series((now() - interval '6 days')::date, now()::date, '1 day')::date as day
),
-- ONE full scan: every count metric (national rollups + per-category cards).
cat_counts as materialized (
  select
    category_main,
    category_type,
    count(*) filter (where is_active = true)::int as active_now,
    count(*) filter (where is_active = false and last_seen_at >= now() - interval '7 days')::int as flipped_7d,
    count(*) filter (where first_seen_at <= now() - interval '7 days'
                       and (is_active = true or last_seen_at >= now() - interval '7 days'))::int as active_7d_ago,
    max(last_seen_at) as last_seen
  from listings_public
  group by category_main, category_type
),
-- ONE recent-slice scan: new listings per (cm, ct, day) over the 14-day window.
new_counts as materialized (
  select category_main, category_type,
    (date_trunc('day', first_seen_at))::date as day,
    count(*)::int as n
  from listings_public
  where first_seen_at >= (now() - interval '13 days')::date
  group by category_main, category_type, (date_trunc('day', first_seen_at))::date
),
-- ONE recent-slice scan: flipped-inactive per (cm, ct, day) over the 7-day window.
flip_counts as materialized (
  select category_main, category_type,
    (date_trunc('day', last_seen_at))::date as day,
    count(*)::int as n
  from listings_public
  where is_active = false and last_seen_at >= (now() - interval '6 days')::date
  group by category_main, category_type, (date_trunc('day', last_seen_at))::date
),
-- Non-listings CTEs carried forward verbatim from migration 136, EXCEPT snap_density,
-- whose per-listing snapshot count is now keyed on listing_id (Gate 2).
snap_density as (
  with counts as (
    select listing_id, count(*) as snap_count
    from listing_snapshots_public
    group by listing_id
  )
  select
    case when snap_count >= 4 then '4+' else snap_count::text end as bucket,
    count(*)::int as n
  from counts
  group by 1
),
freshness_24h as (
  select outcome, count(*)::int as n
  from listing_freshness_checks_public
  where checked_at >= now() - interval '24 hours'
  group by outcome
  order by n desc
),
failures_summary as (
  select
    count(*) filter (where given_up = true)::int as given_up,
    count(*)::int                                as total
  from listing_fetch_failures_public
),
failures_top10 as (
  select sreality_id, attempts, first_failure_at, last_failure_at, given_up
  from listing_fetch_failures_public
  order by attempts desc, last_failure_at desc nulls last
  limit 10
),
cat_failures as (
  select
    l.category_main,
    l.category_type,
    count(*)::int                                  as total,
    count(*) filter (where f.given_up = true)::int as given_up
  from listing_fetch_failures_public f
  join listings_public l on l.sreality_id = f.sreality_id
  group by l.category_main, l.category_type
),
-- Per-category series rebuilt from the pre-aggregated passes (no extra scans).
cat_new_per_day_14 as (
  select cp.category_main, cp.category_type,
    jsonb_agg(jsonb_build_object('day', s.day::text, 'n', coalesce(c.n, 0)) order by s.day) as series
  from category_pairs cp
  cross join series14 s
  left join new_counts c
    on c.category_main = cp.category_main
   and c.category_type = cp.category_type
   and c.day = s.day
  group by cp.category_main, cp.category_type
),
cat_flipped_per_day_7 as (
  select cp.category_main, cp.category_type,
    jsonb_agg(jsonb_build_object('day', s.day::text, 'n', coalesce(c.n, 0)) order by s.day) as series
  from category_pairs cp
  cross join series7 s
  left join flip_counts c
    on c.category_main = cp.category_main
   and c.category_type = cp.category_type
   and c.day = s.day
  group by cp.category_main, cp.category_type
),
by_category as (
  select
    cp.sort_order,
    jsonb_build_object(
      'category_main',       cp.category_main,
      'category_type',       cp.category_type,
      'active_now',          coalesce(cc.active_now, 0),
      'flipped_inactive_7d', coalesce(cc.flipped_7d, 0),
      'new_per_day_14d',     coalesce(npd.series, '[]'::jsonb),
      'flipped_per_day_7d',  coalesce(fpd.series, '[]'::jsonb),
      'failures_total',      coalesce(cf.total,    0),
      'failures_given_up',   coalesce(cf.given_up, 0)
    ) as obj
  from category_pairs cp
  left join cat_counts cc
    on cc.category_main = cp.category_main and cc.category_type = cp.category_type
  left join cat_new_per_day_14 npd
    on npd.category_main = cp.category_main and npd.category_type = cp.category_type
  left join cat_flipped_per_day_7 fpd
    on fpd.category_main = cp.category_main and fpd.category_type = cp.category_type
  left join cat_failures cf
    on cf.category_main = cp.category_main and cf.category_type = cp.category_type
)
select 1 as id, jsonb_build_object(
  'last_scrape_at',         (select max(last_seen) from cat_counts),
  'active_now',             (select coalesce(sum(active_now), 0)::int from cat_counts),
  'active_7d_ago',          (select coalesce(sum(active_7d_ago), 0)::int from cat_counts),
  'flipped_inactive_7d',    (select coalesce(sum(flipped_7d), 0)::int from cat_counts),
  'new_per_day_14d',        coalesce(
                              (select jsonb_agg(jsonb_build_object('day', s.day::text, 'n', coalesce(nd.n, 0)) order by s.day)
                               from series14 s
                               left join (select day, sum(n)::int as n from new_counts group by day) nd on nd.day = s.day),
                              '[]'::jsonb),
  'flipped_per_day_7d',     coalesce(
                              (select jsonb_agg(jsonb_build_object('day', s.day::text, 'n', coalesce(fd.n, 0)) order by s.day)
                               from series7 s
                               left join (select day, sum(n)::int as n from flip_counts group by day) fd on fd.day = s.day),
                              '[]'::jsonb),
  'snapshot_density',       coalesce(
                              (select jsonb_agg(
                                 jsonb_build_object('bucket', bucket, 'n', n)
                                 order by case when bucket = '4+' then 4 else bucket::int end
                               )
                               from snap_density),
                              '[]'::jsonb),
  'freshness_24h',          coalesce(
                              (select jsonb_agg(jsonb_build_object('outcome', outcome, 'n', n))
                               from freshness_24h),
                              '[]'::jsonb),
  'failures_given_up',      (select given_up from failures_summary),
  'failures_total',         (select total    from failures_summary),
  'failures_top10',         coalesce(
                              (select jsonb_agg(jsonb_build_object(
                                 'sreality_id',      sreality_id,
                                 'attempts',         attempts,
                                 'first_failure_at', first_failure_at,
                                 'last_failure_at',  last_failure_at,
                                 'given_up',         given_up
                               ))
                               from failures_top10),
                              '[]'::jsonb),
  'by_category',            coalesce(
                              (select jsonb_agg(obj order by sort_order)
                               from by_category),
                              '[]'::jsonb)
) as payload;

create unique index if not exists health_summary_mv_pk on health_summary_mv (id);

revoke all on health_summary_mv from anon, authenticated;

reset statement_timeout;

-- 1e. properties_public -- 507's body, `district`, `locality_district_id` and
--     `locality_region_id` removed. `district` is the column 506 deliberately
--     spared because region_stats() and region_active_by_day() filtered
--     `district = any(districts_filter)` off this view; section 5 drops both
--     functions, which is what finally frees it (rule 25: the column goes in
--     the PR that removes its last reader).
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
    p.distinct_site_count,
    p.price_drop_count,
    p.price_rise_count,
    p.max_price_drop_pct,
    p.stats_computed_at,
    p.source,
    p.mf_reference_rent_czk,
    p.mf_gross_yield_pct,
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
    p.mf_reference_rent,
    p.published_at,
    p.repr_listing_ref_id as listing_id,
    l.source_id_native,
    measure_price_per_m2_basis(p.category_main, p.category_type) as price_per_m2_basis,
    -- ---- appended by migration 503 (W3 S1) ----
    location_display_label(ll.street_name, ll.house_number_cp, ll.house_number_co,
                           ll.obec_name, ll.cast_obce_name, ll.country_code,
                           ll.country_status) as display_label,
    -- ---- appended by migration 504 (W3 S3) ----
    -- The fourth chip level. There is no legacy twin to re-source from: no
    -- trigger ever wrote a part-of-municipality code onto `listings`, so this
    -- column exists only because `listing_location` answers it.
    ll.cast_obce_kod as cast_obce_id
from properties p
     left join listings l on l.id = p.repr_listing_ref_id
     left join listing_location ll on ll.listing_id = p.repr_listing_ref_id
where p.status = 'active'::text;

revoke all on properties_public from anon, authenticated;
grant select on properties_public to authenticated;

-- 1f. pipeline_board_public -- 506's body, VERBATIM. Not one of its columns
--     changes: `obec`, `obec_id`, `okres_id` and `region_id` come off
--     properties_public, which still serves all four from listing_location.
--     It is re-created only because its parent had to be dropped.
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
  -- ---- appended by migration 503 (W3 S1) ----
  p.display_label,
  -- ---- appended by migration 504 (W3 S3) ----
  p.cast_obce_id
from property_pipeline_public pp
left join properties_public p on p.property_id = pp.property_id;

revoke all on pipeline_board_public from public, anon, authenticated;
grant select on pipeline_board_public to authenticated;

-- 1g. broker_listings_public -- 503's body, `locality` and `district` removed.
--     503 appended `display_label` to this view precisely so the broker
--     inventory table would stop assembling `locality ?? district` by hand.
--     Amendment A6: the broker directory stays dark to browser roles -- the
--     SPA reads it only through the PII-masking /brokers API. Re-state the
--     revoke, never re-grant.
create view broker_listings_public as
select
  bi.broker_id,
  l.sreality_id,
  l.source,
  l.source_url,
  l.category_main,
  l.category_type,
  l.disposition,
  l.area_m2,
  l.price_czk,
  l.is_active,
  l.last_seen_at,
  l.property_id,
  l.subtype,
  l.id as listing_id,
  -- ---- appended by migration 503 (W3 S1) ----
  location_display_label(ll.street_name, ll.house_number_cp, ll.house_number_co,
                         ll.obec_name, ll.cast_obce_name, ll.country_code,
                         ll.country_status) as display_label
from listings l
join broker_identities bi on bi.id = l.broker_identity_id
left join listing_location ll on ll.listing_id = l.id
where bi.broker_id is not null;

revoke all on broker_listings_public from anon, authenticated;

-- 1h. broker_region_type_stats -- 396's body, re-sourced. This is the only
--     matview in the database that reads the trigger-filled
--     listings.{region,okres,obec}_id directly, and it is a HARD dependency:
--     Postgres refuses the column drop while it exists. The codes are
--     value-identical (admin_boundaries.id IS the RUIAN code -- 017:9-12,
--     162:25-27, 171:39-44), so `ll.kraj_kod/okres_kod/obec_kod` is a
--     re-source, not a re-definition: the matview's shape, its two indexes and
--     its explicit domestic-only predicate (`obec` bound => Czech) are
--     unchanged. Refreshed daily by scripts/resolve_brokers.py; populated here
--     so the leaderboard is not empty until that run.
set statement_timeout = '900s';

create materialized view broker_region_type_stats as
 with attributed as (
         select b.id as broker_id,
            ll.kraj_kod  as region_id,
            ll.okres_kod as okres_id,
            ll.obec_kod  as obec_id,
            coalesce(l.category_main, ''::text) as category_main,
            coalesce(l.category_type, ''::text) as category_type,
            coalesce(l.property_id, (- l.id)) as property_key,
            (l.is_active and (l.last_seen_at > (now() - '7 days'::interval))) as is_live
           from (((listings l
             join broker_identities bi on ((bi.id = l.broker_identity_id)))
             join brokers b on (((b.id = bi.broker_id) and (b.status = 'active'::text))))
             join listing_location ll on ((ll.listing_id = l.id)))
          where (ll.obec_kod is not null)
        ), per_level as (
         select 'region'::text as geo_level,
            attributed.region_id as geo_id,
            attributed.broker_id,
            attributed.category_main,
            attributed.category_type,
            attributed.property_key,
            attributed.is_live
           from attributed
          where (attributed.region_id is not null)
        union all
         select 'okres'::text,
            attributed.okres_id,
            attributed.broker_id,
            attributed.category_main,
            attributed.category_type,
            attributed.property_key,
            attributed.is_live
           from attributed
          where (attributed.okres_id is not null)
        union all
         select 'obec'::text,
            attributed.obec_id,
            attributed.broker_id,
            attributed.category_main,
            attributed.category_type,
            attributed.property_key,
            attributed.is_live
           from attributed
          where (attributed.obec_id is not null)
        )
 select broker_id,
    geo_level,
    geo_id,
    category_main,
    category_type,
    count(*) as listing_count,
    count(distinct property_key) as property_count,
    count(*) filter (where is_live) as active_listing_count,
    count(distinct property_key) filter (where is_live) as active_property_count
   from per_level
  group by broker_id, geo_level, geo_id, category_main, category_type;

create unique index if not exists broker_region_type_stats_pk on broker_region_type_stats
  using btree (broker_id, geo_level, geo_id, category_main, category_type);
create index if not exists broker_region_type_stats_rank_idx on broker_region_type_stats
  using btree (geo_level, geo_id, category_main, category_type, active_property_count desc);
revoke all on broker_region_type_stats from anon, authenticated;

reset statement_timeout;

-- 1i. broker_geo_options -- 396's body, VERBATIM. Re-created only because its
--     matview had to be dropped.
create view broker_geo_options as
select s.geo_level, s.geo_id, ab.name, ab.parent_id,
       count(distinct s.broker_id) as broker_count
from broker_region_type_stats s
join admin_boundaries ab on ab.id = s.geo_id
where s.geo_level in ('region', 'okres')
group by s.geo_level, s.geo_id, ab.name, ab.parent_id;
revoke all on broker_geo_options from anon, authenticated;

-- 1j. broker_leaderboard(...) -- 469's body with ONE change: the price/subtype
--     branch's `live_priced` CTE reads its three admin codes off
--     listing_location instead of the listings columns. `language sql` means
--     Postgres compiles this body at CREATE time, so CI's schema replay is the
--     proof it no longer names a dropped column. Signature unchanged, so the
--     ACL survives -- 469's three revokes are re-stated anyway, because a new
--     overload would otherwise inherit PUBLIC EXECUTE (Amendment A6).
create or replace function public.broker_leaderboard(
  p_region_ids bigint[] default null::bigint[],
  p_okres_ids bigint[] default null::bigint[],
  p_obec_ids bigint[] default null::bigint[],
  p_category_main text default null::text,
  p_category_type text default null::text,
  p_metric text default 'active_property_count'::text,
  p_limit integer default 100,
  p_firm_ids bigint[] default null::bigint[],
  p_min_price_czk integer default null::integer,
  p_include_unpriced boolean default false,
  p_subtypes text[] default null::text[],
  p_include_unknown_subtype boolean default false
)
returns table(broker_id bigint, display_name text, primary_email text, primary_phone text,
              firm_id bigint, firm_name text, firm_domain text,
              listing_count bigint, property_count bigint,
              active_listing_count bigint, active_property_count bigint)
language sql
stable
as $function$
  -- Shared by both branches: computed once regardless of which branch's gate is true,
  -- since it references neither broker_region_type_stats nor listings (448's header
  -- measures what duplicating it per branch cost).
  with active_brokers as materialized (
    select b.id
    from brokers b
    where b.status = 'active'
      and (p_firm_ids is null or b.primary_firm_id = any(p_firm_ids))
  ),

  -- FAST BRANCH: the precomputed matview. Gated on "no live-only filter is set" — every
  -- arm repeats the full gate so the planner can prove the whole branch dead when one is.
  fast_raw as (
    select s.broker_id, s.listing_count, s.property_count,
           s.active_listing_count, s.active_property_count
    from broker_region_type_stats s
    where p_min_price_czk is null and p_subtypes is null
      and coalesce(array_length(p_region_ids, 1), 0)
              + coalesce(array_length(p_okres_ids, 1), 0)
              + coalesce(array_length(p_obec_ids, 1), 0) = 0
      and s.geo_level = 'region'
      and (p_category_main is null or s.category_main = p_category_main)
      and (p_category_type is null or s.category_type = p_category_type)
    union all
    select s.broker_id, s.listing_count, s.property_count,
           s.active_listing_count, s.active_property_count
    from broker_region_type_stats s
    where p_min_price_czk is null and p_subtypes is null
      and coalesce(array_length(p_region_ids, 1), 0) > 0
      and s.geo_level = 'region'
      and s.geo_id = any(coalesce(p_region_ids, '{}'::bigint[]))
      and (p_category_main is null or s.category_main = p_category_main)
      and (p_category_type is null or s.category_type = p_category_type)
    union all
    select s.broker_id, s.listing_count, s.property_count,
           s.active_listing_count, s.active_property_count
    from broker_region_type_stats s
    where p_min_price_czk is null and p_subtypes is null
      and coalesce(array_length(p_okres_ids, 1), 0) > 0
      and s.geo_level = 'okres'
      and s.geo_id = any(coalesce(p_okres_ids, '{}'::bigint[]))
      and (p_category_main is null or s.category_main = p_category_main)
      and (p_category_type is null or s.category_type = p_category_type)
    union all
    select s.broker_id, s.listing_count, s.property_count,
           s.active_listing_count, s.active_property_count
    from broker_region_type_stats s
    where p_min_price_czk is null and p_subtypes is null
      and coalesce(array_length(p_obec_ids, 1), 0) > 0
      and s.geo_level = 'obec'
      and s.geo_id = any(coalesce(p_obec_ids, '{}'::bigint[]))
      and (p_category_main is null or s.category_main = p_category_main)
      and (p_category_type is null or s.category_type = p_category_type)
  ),
  fast_agg as (
    select r.broker_id,
           sum(r.listing_count)::bigint          as listing_count,
           sum(r.property_count)::bigint         as property_count,
           sum(r.active_listing_count)::bigint   as active_listing_count,
           sum(r.active_property_count)::bigint  as active_property_count
    from fast_raw r
    join active_brokers ab on ab.id = r.broker_id
    group by r.broker_id
  ),
  fast_top as (
    select a.broker_id, a.listing_count, a.property_count,
           a.active_listing_count, a.active_property_count
    from fast_agg a
    order by case p_metric
               when 'listing_count'        then a.listing_count
               when 'property_count'       then a.property_count
               when 'active_listing_count' then a.active_listing_count
               else                             a.active_property_count
             end desc,
             a.broker_id
    limit greatest(1, least(p_limit, 2000))
  ),

  -- LIVE BRANCH: reads `listings` directly, for the filters the matview cannot express
  -- (price since 448, subtype since 469). Mirrors the matview's own base predicate
  -- (obec_id is not null = migration 396's domestic scope, is_active + 7-day window =
  -- "active"), then adds those filters. Both are written `p_x is null or <match>` so each
  -- is inert when unset — the branch now runs whenever EITHER is set.
  --
  -- `listing_location` carries kraj/okres/obec codes on one row per listing, so matching
  -- "any selected admin unit at any level" is a plain OR across three columns here — no
  -- need for the matview's per-level UNION ALL explosion above, which exists only because
  -- THAT table stores one row per (broker, level, id). W4-c moved this off the trigger-
  -- filled listings.{region,okres,obec}_id; the codes are value-identical.
  live_priced as (
    select l.broker_identity_id,
           coalesce(l.property_id, -l.id) as property_key,
           (l.is_active and l.last_seen_at > now() - interval '7 days') as is_live
    from listings l
    join listing_location ll on ll.listing_id = l.id
    where (p_min_price_czk is not null or p_subtypes is not null)
      and l.broker_identity_id is not null
      and ll.obec_kod is not null
      and (p_category_main is null or l.category_main = p_category_main)
      and (p_category_type is null or l.category_type = p_category_type)
      and (
        coalesce(array_length(p_region_ids, 1), 0)
          + coalesce(array_length(p_okres_ids, 1), 0)
          + coalesce(array_length(p_obec_ids, 1), 0) = 0
        or ll.kraj_kod  = any(coalesce(p_region_ids, '{}'::bigint[]))
        or ll.okres_kod = any(coalesce(p_okres_ids,  '{}'::bigint[]))
        or ll.obec_kod  = any(coalesce(p_obec_ids,   '{}'::bigint[]))
      )
      and (
        p_min_price_czk is null
        or l.price_czk >= p_min_price_czk
        or (l.price_czk is null and p_include_unpriced)
      )
      and (
        p_subtypes is null
        or l.subtype = any(p_subtypes)
        or (l.subtype is null and p_include_unknown_subtype)
      )
  ),
  live_agg as (
    select bi.broker_id,
           count(*)                                                as listing_count,
           count(distinct p.property_key)                          as property_count,
           count(*) filter (where p.is_live)                       as active_listing_count,
           count(distinct p.property_key) filter (where p.is_live) as active_property_count
    from live_priced p
    join broker_identities bi on bi.id = p.broker_identity_id
    join active_brokers ab on ab.id = bi.broker_id
    group by bi.broker_id
  ),
  live_top as (
    select a.broker_id, a.listing_count, a.property_count,
           a.active_listing_count, a.active_property_count
    from live_agg a
    order by case p_metric
               when 'listing_count'        then a.listing_count
               when 'property_count'       then a.property_count
               when 'active_listing_count' then a.active_listing_count
               else                             a.active_property_count
             end desc,
             a.broker_id
    limit greatest(1, least(p_limit, 2000))
  ),

  -- Hydration for both branches (each ..._top is already <= p_limit rows), then one
  -- combined result. The two guards are exact logical complements, so exactly one branch
  -- ever contributes rows — the union selects between two complete answers, it does not
  -- merge two partial ones.
  combined as (
    select t.broker_id, b.display_name, b.primary_email, b.primary_phone,
           b.primary_firm_id      as firm_id,
           f.display_name         as firm_name,
           f.canonical_domain     as firm_domain,
           t.listing_count, t.property_count,
           t.active_listing_count, t.active_property_count
    from fast_top t
    join brokers b on b.id = t.broker_id
    left join firms f on f.id = b.primary_firm_id
    where p_min_price_czk is null and p_subtypes is null
    union all
    select t.broker_id, b.display_name, b.primary_email, b.primary_phone,
           b.primary_firm_id      as firm_id,
           f.display_name         as firm_name,
           f.canonical_domain     as firm_domain,
           t.listing_count, t.property_count,
           t.active_listing_count, t.active_property_count
    from live_top t
    join brokers b on b.id = t.broker_id
    left join firms f on f.id = b.primary_firm_id
    where p_min_price_czk is not null or p_subtypes is not null
  )
  -- Explicit final ORDER BY, even though each branch already emits its rows in the right
  -- order and exactly one branch is ever non-empty: an incidental guarantee is not the
  -- same promise as an explicit one (migration 435's tiebreaker rationale).
  select * from combined
  order by case p_metric
             when 'listing_count'        then listing_count
             when 'property_count'       then property_count
             when 'active_listing_count' then active_listing_count
             else                             active_property_count
           end desc,
           broker_id
$function$;

revoke execute on function public.broker_leaderboard(
  bigint[], bigint[], bigint[], text, text, text, integer, bigint[], integer, boolean,
  text[], boolean) from public;
revoke execute on function public.broker_leaderboard(
  bigint[], bigint[], bigint[], text, text, text, integer, bigint[], integer, boolean,
  text[], boolean) from anon;
revoke execute on function public.broker_leaderboard(
  bigint[], bigint[], bigint[], text, text, text, integer, bigint[], integer, boolean,
  text[], boolean) from authenticated;

-- 1k. data_quality_by_source -- 495's body; the seven legacy place probes
--     become three read off listing_location. Same five output columns, same
--     is_platform_admin() gate, so `create or replace` keeps the ACL.
create or replace view public.data_quality_by_source as
select * from (SELECT l.source,
    v.field,
    count(*) AS n_active,
    count(*) FILTER (WHERE v.present) AS n_populated,
    round(100.0 * count(*) FILTER (WHERE v.present)::numeric / count(*)::numeric, 1) AS pct_populated
   FROM listings l
     LEFT JOIN listing_location ll ON ll.listing_id = l.id
     CROSS JOIN LATERAL ( VALUES ('price_czk'::text,l.price_czk IS NOT NULL), ('area_m2'::text,l.area_m2 IS NOT NULL), ('disposition'::text,l.disposition IS NOT NULL), ('category_main'::text,l.category_main IS NOT NULL), ('category_type'::text,l.category_type IS NOT NULL), ('geom'::text,ll.geom IS NOT NULL), ('locality'::text,ll.obec_name IS NOT NULL), ('street'::text,ll.street_name IS NOT NULL), ('floor'::text,l.floor IS NOT NULL), ('total_floors'::text,l.total_floors IS NOT NULL), ('has_balcony'::text,l.has_balcony IS NOT NULL), ('has_lift'::text,l.has_lift IS NOT NULL), ('has_parking'::text,l.has_parking IS NOT NULL), ('terrace'::text,l.terrace IS NOT NULL), ('cellar'::text,l.cellar IS NOT NULL), ('garage'::text,l.garage IS NOT NULL), ('parking_lots'::text,l.parking_lots IS NOT NULL), ('building_type'::text,l.building_type IS NOT NULL), ('condition'::text,l.condition IS NOT NULL), ('energy_rating'::text,l.energy_rating IS NOT NULL), ('furnished'::text,l.furnished IS NOT NULL), ('ownership'::text,l.ownership IS NOT NULL), ('building_condition_level'::text,l.building_condition_level IS NOT NULL), ('apartment_condition_level'::text,l.apartment_condition_level IS NOT NULL), ('property_grouped'::text,l.property_id IS NOT NULL), ('source_url'::text,l.source_url IS NOT NULL)) v(field, present)
  WHERE l.is_active
  GROUP BY l.source, v.field
) __admin_gate
where is_platform_admin();

-- 1l. recompute_city_proximity(boolean) -- 142's body, re-sourced onto
--     listing_location through `properties.repr_listing_ref_id`. It is the
--     only writer of home_obec_pop / near_*_{5,15}km, which Browse filters
--     read, and its only legacy input was `properties.geom`. The anchor set
--     (admin_boundaries polygons + population) is unchanged -- that is the
--     table's surviving role. `ll.geom` is geometry(Point,4326) and
--     `admin_boundaries.geom` is geography, so every probe casts
--     `ll.geom::geography`, served by listing_location_geog_gist (507).
--     BEHAVIOUR NOTE: a merged property's proximity now comes from its
--     representative listing's point rather than a per-property `best_geo`
--     pick, which is the same rule W4-a applied to its street and its stats.
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
    join listing_location ll on ll.listing_id = p2.repr_listing_ref_id
    left join lateral (
      select ab.population from admin_boundaries ab
      where ab.level = 'obec'
      order by ll.geom::geography <-> ab.geom
      limit 1
    ) hp on true
    left join lateral (
      select max(population) as pop, max(jobs) as jobs, max(youth) as youth, max(overall) as overall
      from _prox_anchors a where st_dwithin(ll.geom::geography, a.geom, 5000)
    ) a5 on true
    left join lateral (
      select max(population) as pop, max(jobs) as jobs, max(youth) as youth, max(overall) as overall
      from _prox_anchors a where st_dwithin(ll.geom::geography, a.geom, 15000)
    ) a15 on true
    where ll.geom is not null
      and (p_full or p2.city_proximity_computed_at is null)
  ) s
  where p.id = s.id;

  get diagnostics n = row_count;
  return n;
end
$$;

-- ---------------------------------------------------------------------------
-- 2. The write-side machinery: triggers, then their functions, then the two
--    CHECKs, then the named indexes.
--
--    Order is load-bearing because each statement autocommits.
--    `trg_listings_geo_cell_key` goes FIRST: its `UPDATE OF` list names
--    `obec_id` (276:119-120), so it must be gone before trigger 289 -- which
--    WRITES obec_id -- and before any column drop.
--
--    Trigger 289 is the whole geo-derivation epoch in one function: it PIPs
--    admin_boundaries with a 250 m sliver fallback to fill
--    obec/okres/region/*_id/ku_id, fills `district` when NULL, and nulls the
--    resolver's street on a coordinate move. Every one of those outputs is a
--    column this file drops; the resolver lane (listing_location) computes the
--    same chain from RUIAN, once, with a stated confidence.
--
--    The indexes are belt-and-braces: `alter table ... drop column` drops any
--    index that names the column anyway. They are listed explicitly so the
--    file reads as the full inventory of what disappears, and because
--    `drop index if exists` costs nothing. Five of them
--    (`*_id_idx`, migration 344) are the maintenance-walker forms that
--    scripts/apply_r2_maintenance_indexes.py kept in sync; that script is
--    deleted in this PR's code half.
-- ---------------------------------------------------------------------------

drop trigger if exists trg_listings_geo_cell_key on listings;
drop trigger if exists trg_listings_admin_geo on listings;
drop trigger if exists properties_set_latlng_trg on properties;

drop function if exists listings_set_geo_cell_key();
drop function if exists listing_geo_cell_key(bigint, geography, text, text);
drop function if exists listings_set_admin_geo();
drop function if exists properties_set_latlng();

alter table listings drop constraint if exists listings_geo_cell_key_format;
alter table listings drop constraint if exists listings_street_key_presence;

drop index if exists listings_obec_id_idx;
drop index if exists listings_okres_id_idx;
drop index if exists listings_region_id_idx;
drop index if exists listings_ku_id_idx;
drop index if exists listings_geom_idx;
drop index if exists listings_geocode_candidates_idx;
drop index if exists listings_geocode_candidates_id_idx;
drop index if exists listings_geo_cell_key_idx;
drop index if exists listings_geo_cell_key_null_idx;
drop index if exists listings_geo_cell_key_null_id_idx;
drop index if exists listings_geo_cell_key_byt_null_idx;
drop index if exists listings_geo_cell_key_byt_null_id_idx;
drop index if exists listings_street_name_key_null_idx;
drop index if exists listings_street_name_key_null_id_idx;
drop index if exists listings_source_active_street_idx;
drop index if exists listings_source_active_street_id_idx;
drop index if exists listings_locality_district_id_idx;
drop index if exists listings_locality_region_id_idx;
drop index if exists listings_locality_municipality_id_idx;
drop index if exists listings_locality_quarter_id_idx;
drop index if exists listings_locality_ward_id_idx;

drop index if exists properties_geom_idx;
drop index if exists properties_region_id_idx;
drop index if exists properties_okres_id_idx;
drop index if exists properties_obec_id_idx;

-- ---------------------------------------------------------------------------
-- 3. properties -- the property's geography set.
--
--    One ALTER, one ACCESS EXCLUSIVE acquisition on a 635k-row table.
--    `place_search_text` leads the list even though 506 already dropped it:
--    it was GENERATED ALWAYS over `street`/`locality`, so on any schema where
--    it survives, dropping those two first would error. `if exists` makes the
--    clause a notice on the schema where it is already gone.
--
--    After this the property has no place of its own at all. Its place is a
--    join through `repr_listing_ref_id` to listing_location -- which is what
--    properties_public, browse_projection and recompute_city_proximity above
--    now do, and what W4-a already did to the street and the stats. The
--    per-property `best_geo`/`best_street` pickers that used to fill these
--    (a merged property's street could come from a different child than its
--    price) are gone with them.
-- ---------------------------------------------------------------------------

alter table properties
  drop column if exists place_search_text,
  drop column if exists geom,
  drop column if exists lat,
  drop column if exists lng,
  drop column if exists district,
  drop column if exists locality,
  drop column if exists street,
  drop column if exists obec,
  drop column if exists okres,
  drop column if exists region,
  drop column if exists obec_id,
  drop column if exists okres_id,
  drop column if exists region_id,
  drop column if exists locality_district_id,
  drop column if exists locality_region_id,
  drop column if exists ku_id;

-- ---------------------------------------------------------------------------
-- 4. listings -- all 24, in ONE statement.
--
--    ONE ALTER = ONE ACCESS EXCLUSIVE acquisition on the hottest table in the
--    database, rather than 24 chances to queue behind a reader. Catalog-only:
--    Postgres marks each attribute dropped and rewrites no heap, so the
--    statement is milliseconds once the lock is granted. The space is
--    reclaimed lazily by autovacuum, not here.
-- ---------------------------------------------------------------------------

alter table listings
  drop column if exists geom,
  drop column if exists obec_id,
  drop column if exists okres_id,
  drop column if exists region_id,
  drop column if exists ku_id,
  drop column if exists locality,
  drop column if exists district,
  drop column if exists obec,
  drop column if exists okres,
  drop column if exists region,
  drop column if exists street,
  drop column if exists house_number,
  drop column if exists zip,
  drop column if exists street_id,
  drop column if exists locality_municipality_id,
  drop column if exists locality_quarter_id,
  drop column if exists locality_ward_id,
  drop column if exists locality_district_id,
  drop column if exists locality_region_id,
  drop column if exists street_name_key,
  drop column if exists street_source,
  drop column if exists geo_cell_key,
  drop column if exists coord_street_attempt_version,
  drop column if exists geocode_attempted_at;

-- ---------------------------------------------------------------------------
-- 5. The stores and the RPCs that only ever existed to serve those columns.
--
--    geocode_cache (288) -- 1,328 rows, last written 2026-07-11. The Mapy
--      geocoder that filled it went in W4-b.
--    mapy_affected / _cache / _props / mapy_inventory_runs (385) -- the
--      licence inventory that policed which coordinates came from Mapy. W4-b
--      removed the veto from the claims lane (`assert_inventory_ready` and
--      the three LEFT JOINs), so nothing reads them. The three children carry
--      an FK to mapy_inventory_runs, so they go first; each table takes its own
--      immutability trigger with it, and mapy_inventory_immutable() is dropped
--      by name after (a function is not a dependent of the tables its triggers
--      sit on). No CASCADE anywhere: if something still depends on one of
--      these, this file should FAIL, not silently take it too.
--    address_points / address_points_revisions (196/222) -- 1.5M rows, the
--      pre-RUIAN address mirror. One writer (scripts/ingest_address_points.py,
--      cron 30 3 3 * *) and one reader (backfill_address_point_streets.py),
--      both deleted in this PR. ruian_address_points (381, 3.0M rows) is the
--      replacement and already serves the resolver. The coord -> nearest
--      street-point capability is NOT reproduced in RUIAN; nothing schedules
--      it, so that is an accepted loss, recorded here rather than migrated.
--    region_stats / region_active_by_day -- filtered `district = any(...)`, a
--      legacy NAME array, off properties_public. Zero callers in api/,
--      toolkit/, frontend/src/, scripts/ or the extension.
--    browse_stats -- migration 083's 46-argument listing-grain Browse RPC,
--      superseded by browse_stats_properties, EXECUTE revoked in 428, and
--      commented by 425 with "the intended end state is DROP, held back only
--      for want of operator sign-off on a destructive step". It read
--      listings.district/locality.
--
--    All three RPCs are dropped through pg_proc by NAME rather than by a
--    transcribed signature -- region_stats alone carries a 40-plus parameter
--    list across two overloads (103, 425), and hand-transcribing a signature
--    is how a drop silently targets nothing (migration 428's own header).
-- ---------------------------------------------------------------------------

drop table if exists geocode_cache;

drop table if exists mapy_affected_cache;
drop table if exists mapy_affected_props;
drop table if exists mapy_affected;
drop table if exists mapy_inventory_runs;
drop function if exists mapy_inventory_immutable();

drop table if exists address_points_revisions;
drop table if exists address_points;

do $$
declare
  r record;
  n int := 0;
begin
  for r in
    select p.oid::regprocedure::text as sig
      from pg_proc p
      join pg_namespace ns on ns.oid = p.pronamespace
     where ns.nspname = 'public'
       and p.proname in ('region_stats', 'region_active_by_day', 'browse_stats')
  loop
    execute format('drop function %s', r.sig);
    n := n + 1;
  end loop;
  raise notice '508: dropped % legacy stats RPC overload(s)', n;
end
$$;

-- ---------------------------------------------------------------------------
-- 6. The read models, the schema cache, and the proof.
--
--    `browse_list` is a TABLE built CTAS from browse_projection and patched by
--    `sync_browse_list`'s POSITIONAL `INSERT ... SELECT * FROM
--    browse_projection` (toolkit/browse_read_model.py, pinned verbatim at
--    tests/test_browse_read_model.py). Until it is rebuilt it is two columns
--    WIDER than the view, and the next merge would write every value one
--    position out. `properties_map_mv` does not exist at all until
--    rebuild_properties_map_mv() runs (section 1 dropped it). The */15 and
--    7,37 cron ticks would close both within ~20 minutes; closing them here
--    means the schema and the read models are never out of step.
--
--    statement_timeout is raised the way the two cron jobs raise it
--    (migrations 413 / 371): the `postgres` role this file is applied as
--    reports 2 min and the rebuild averages 270 s.
-- ---------------------------------------------------------------------------

set statement_timeout = '900s';
set lock_timeout = '30s';

select rebuild_browse_list();
select rebuild_properties_map_mv();

reset statement_timeout;
set lock_timeout = '5s';

select pg_notify('pgrst', 'reload schema');

-- Both rebuilds SKIP with a NOTICE (not an error) when a cron tick already
-- holds their advisory lock. For browse_list a skip is the dangerous case --
-- the table keeps its two extra columns and the next positional merge writes
-- NULLs into them; for properties_map_mv a skip leaves the matview MISSING.
do $$
declare v_left text;
begin
  select string_agg(format('%s.%s', t, a), ', ') into v_left from (
    select 'browse_list' as t, a.attname as a
      from pg_attribute a
     where a.attrelid = 'public.browse_list'::regclass
       and not a.attisdropped and a.attnum > 0
       and a.attname in ('locality_district_id', 'locality_region_id')
    union all
    select 'properties_map_mv', a.attname
      from pg_attribute a
     where a.attrelid = 'public.properties_map_mv'::regclass
       and not a.attisdropped and a.attnum > 0
       and a.attname in ('locality_district_id', 'locality_region_id')
  ) s;
  if v_left is not null then
    raise exception
      'read model(s) still carry the dropped W4-c columns (%) -- a concurrent '
      'rebuild held the advisory lock and this one skipped; pause the browse '
      'rebuild cron and re-run rebuild_browse_list() / '
      'rebuild_properties_map_mv()', v_left;
  end if;
end $$;

-- The proof. Every column, table and function this file claims to remove is
-- checked against the catalog by name; anything still standing is listed in
-- one exception rather than left for a reader to discover.
do $$
declare
  v_left text;
  c_listings constant text[] := array[
    'geom','obec_id','okres_id','region_id','ku_id','locality','district','obec',
    'okres','region','street','house_number','zip','street_id',
    'locality_municipality_id','locality_quarter_id','locality_ward_id',
    'locality_district_id','locality_region_id','street_name_key','street_source',
    'geo_cell_key','coord_street_attempt_version','geocode_attempted_at'];
  c_properties constant text[] := array[
    'place_search_text','geom','lat','lng','district','locality','street','obec',
    'okres','region','obec_id','okres_id','region_id','locality_district_id',
    'locality_region_id','ku_id'];
  t_gone constant text[] := array[
    'geocode_cache','mapy_affected','mapy_affected_cache','mapy_affected_props',
    'mapy_inventory_runs','address_points','address_points_revisions'];
  f_gone constant text[] := array[
    'mapy_inventory_immutable','region_stats','region_active_by_day','browse_stats',
    'listings_set_admin_geo','listings_set_geo_cell_key','listing_geo_cell_key',
    'properties_set_latlng'];
begin
  select string_agg(x, ', ' order by x) into v_left from (
    select 'listings.' || a.attname as x
      from pg_attribute a
     where a.attrelid = 'public.listings'::regclass
       and not a.attisdropped and a.attnum > 0
       and a.attname = any(c_listings)
    union all
    select 'properties.' || a.attname
      from pg_attribute a
     where a.attrelid = 'public.properties'::regclass
       and not a.attisdropped and a.attnum > 0
       and a.attname = any(c_properties)
    union all
    select 'table ' || c.relname
      from pg_class c join pg_namespace n on n.oid = c.relnamespace
     where n.nspname = 'public' and c.relname = any(t_gone)
    union all
    select 'function ' || p.proname
      from pg_proc p join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public' and p.proname = any(f_gone)
    union all
    select 'trigger ' || t.tgname
      from pg_trigger t
     where not t.tgisinternal
       and t.tgname in ('trg_listings_admin_geo', 'trg_listings_geo_cell_key',
                        'properties_set_latlng_trg')
  ) s;
  if v_left is not null then
    raise exception 'W4-c did not finish -- these survive: %', v_left;
  end if;
end $$;
