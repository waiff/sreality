-- 506_location_w3_s4_deletions.sql
--
-- W3 S4 -- the deletions rule 25 requires now that every place DISPLAY reads
-- `display_label` (S1+S2, migration 503) and every place FILTER is one code
-- predicate (S3, migration 504). Nothing here changes behaviour: every column
-- this file removes has already lost its last reader on the branch that lands
-- with it. Rule 25: "every location PR deletes at least as much as it adds" --
-- this one only deletes.
--
-- WHAT COMES OUT, AND WHY IT CAN
--
--   place_search_text   the free-text half of the retired chip predicate
--                       (`... ILIKE '%needle%'`, migration 182). S3 replaced the
--                       five predicates with `<level>_id = any(codes)`; the only
--                       readers left were the four view bodies below plus a
--                       SPA row type that carried it and rendered nothing.
--                       Also the generated column on `properties` (migration
--                       302), whose readers were the removed dedup surfaces.
--
--   locality/district/  the legacy place TEXT. S2 collapsed eleven rendering
--   street/obec/        sites onto `display_label`; `browse_list` and
--   okres/region        `properties_map_mv` are selected by three explicit
--                       column lists (MAP_COLS / TABLE_COLS / CARD_COLS) and
--                       none of them names one. TWO SURVIVE, both on
--                       properties_public: `obec`, because the board's
--                       "Mesto A-Z" sort orders by the TOWN, which is the tail
--                       of the label rather than its head (lib/pipelineSort);
--                       and `district`, because `region_stats()` /
--                       `region_active_by_day()` still filter on it BY NAME.
--                       A column is deleted in the PR that removes its last
--                       reader -- these two still have one.
--
--   home_city_id        migration 375's precomputed curated-city key. Migration
--                       436 re-keyed city-quality membership onto `obec_id` via
--                       curated_cities_matching(), leaving it exactly ONE
--                       reader: listings_with_city_quality(), reachable only
--                       through the `?cityQualityLegacy=1` bisect hatch that
--                       this same PR deletes. The function is dropped below, so
--                       the column really does end with no reader rather than
--                       with a stranded one. Its job (recompute_home_city.yml, daily,
--                       measured at 680 MB of buffer traffic per call) and its
--                       driver go with it, so `home_city_computed_at` and
--                       `recompute_home_city()` -- which exist only to schedule
--                       that job incrementally -- go too.
--
-- THE LEGACY `listings` COLUMNS ARE NOT TOUCHED. `listings.obec_id`, `geom`,
-- `street`, `locality`, `district` and the rest still serve listings_public,
-- listing_feed_public's own place text, broker_listings_public, the comparables
-- radius and every backfill script. They are W4's list (rule 24), not this one's.
--
-- WHY DROP + CREATE AND NOT `CREATE OR REPLACE`
--
-- `create or replace view` can only APPEND: Postgres refuses to drop, rename,
-- reposition or retype an existing output column. Removing one therefore means
-- DROP VIEW then CREATE VIEW, in dependency order, which has three consequences
-- this file handles explicitly:
--
--   1. `properties_map_mv` is a materialized view over `browse_projection` and
--      `pipeline_board_public` is a view over `properties_public`; both must be
--      dropped first and rebuilt/recreated here.
--   2. A dropped view loses its ACL, and this project's default privileges in
--      `public` GRANT the new one to `anon` AND `authenticated` on creation.
--      Every revoke/grant pair below is therefore LOAD-BEARING, not decoration
--      (migration 299's posture; tests/test_migration_rls_grants.py and
--      tests/location_data/test_location_schema_contracts.py pin it). Each
--      revoke names BOTH browser roles -- wider than 503's, which could revoke
--      anon alone because `create or replace view` preserves the ACL and there
--      was nothing new to take away. Here a default-granted INSERT/UPDATE/
--      DELETE to `authenticated` would survive a `revoke ... from anon`. The
--      `grant select` that follows each one puts back exactly the read the
--      browser role is supposed to have, and nothing else.
--   3. `browse_list` is a TABLE (blue-green CTAS), so it survives the view drop
--      carrying eight columns the view no longer has. Until it is rebuilt,
--      `sync_browse_list`'s POSITIONAL `insert into browse_list select * from
--      browse_projection` (toolkit/browse_read_model.py) would leave those eight
--      NULL on a merged property -- Postgres allows fewer expressions than
--      target columns and says nothing. Both read models are therefore rebuilt
--      at the foot of this file, in the same apply, exactly as migration 503
--      did, and the rebuild is ASSERTED afterwards rather than assumed.
--
-- ORDER IS PRESERVED. What survives keeps the relative order migrations 503/504
-- left it in, minus the removed names -- `sync_browse_list` is positional, so a
-- "tidy" reorder would write every value into the wrong column.
-- tests/test_location_w3_projection.py::test_s4_drops_only pins this both ways.
--
-- ci-allow-dynamic: rebuild_properties_map_mv blue-greens its materialized view
-- through `EXECUTE`'d DDL on every tick and has since migration 277, so the
-- offline scanner in tests/test_migration_rls_grants.py cannot inspect it
-- (dollar-quoted bodies are opaque there by design). That blind spot is covered
-- by the DEDICATED gate tests/test_browse_grant_drift.py, which reaches inside
-- the EXECUTE strings for exactly this relation and fails on any `anon`
-- re-grant, plus the runtime self-check inside the body itself. Same
-- annotation, same reasoning as migrations 371/376/440.
--
-- APPLY AFTER THE DEPLOY, NOT BEFORE -- the opposite of S1 (503/504), and the
-- direction is forced by which side is additive. 503 ADDED columns, so the
-- schema had to lead the bundle that selected them. This one REMOVES columns,
-- so the bundle that stops selecting them has to lead the schema: the SPA
-- currently selects `place_search_text, obec, locality, okres, region, district`
-- off `pipeline_board_public`, and PostgREST answers a select for a column the
-- view no longer has with a 400 -- the pipeline board would be broken for the
-- length of the Railway rollout. In the other order nothing breaks at all: a
-- view that still carries a column nobody selects is inert. Since a merge to
-- `main` IS the deploy, the natural sequence is merge -> Railway rolls out ->
-- apply this. Do NOT pre-apply it on the branch.
--
-- Then: the forced rebuilds at the foot take ~5 minutes, and
-- `properties_map_mv` DOES NOT EXIST between its drop in section 1 and
-- `rebuild_properties_map_mv()` in section 8 -- the Browse map errors for that
-- window, so apply off-peak. `browse_list` is rebuilt FIRST even though the map
-- is the user-visible outage: until it is rebuilt a merge writes NULLs into its
-- eight extra columns silently (consequence 3 below), and silent wrong data
-- outranks a loud five-minute error.

begin;

-- A DROP VIEW needs ACCESS EXCLUSIVE on the view, and both rebuild functions
-- hold locks on browse_projection for minutes at a time. Fail fast instead of
-- queueing in front of every reader (the hot-table DDL rule): if this aborts,
-- pause the `*/15` browse rebuild cron (or wait for the gap) and re-run.
set local lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 1. Drop, in dependency order: dependents before the relations they read.
-- ---------------------------------------------------------------------------

drop view if exists pipeline_board_public;
drop materialized view if exists properties_map_mv;
drop view if exists properties_public;
drop view if exists browse_projection;
drop view if exists listing_feed_public;

-- ---------------------------------------------------------------------------
-- 2. browse_projection -- migration 503's body, eight columns REMOVED.
--    `browse_list` and `properties_map_mv` both materialize `select * from
--    browse_projection`, so they inherit the narrowing at the rebuilds forced
--    at the foot of this file.
-- ---------------------------------------------------------------------------

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
    p.locality_district_id,
    p.locality_region_id,
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

-- ---------------------------------------------------------------------------
-- 3. listing_feed_public -- the portal-mirror lane (migration 503), one column
--    REMOVED. Its own legacy place text (`locality`/`district`/`obec`/`okres`/
--    `region`/`street`/`house_number`) stays: those are `listings` columns, W4's
--    list, and this view is the listing-grain mirror that still exposes them.
-- ---------------------------------------------------------------------------

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
  l.locality,
  l.district,
  l.obec,
  l.okres,
  l.region,
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
  l.street,
  l.house_number,
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

-- ---------------------------------------------------------------------------
-- 4. properties_public -- migration 504's body, seven columns REMOVED. `obec`
--    stays (see the header: the board's town sort). The Watchdog reads this
--    view, and its place predicate is `district_where`'s four codes
--    (api/location_filter.py) plus `ST_DWithin` rebuilt from `lat`/`lng` --
--    none of the seven.
-- ---------------------------------------------------------------------------

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
    -- KEPT, unlike on browse_projection: `region_stats()` and
    -- `region_active_by_day()` (migrations 425 / 103) still filter
    -- `district = any(districts_filter)` -- a legacy NAME array -- on this view.
    -- Neither has a caller in api/, toolkit/, frontend/src/, scripts/ or the
    -- extension (migration 425 verified that before it re-created region_stats),
    -- but CI's schema-replay lane exercises both against a real database, and
    -- rule 25 deletes a column in the PR that removes its last READER, not
    -- before. W4 re-points or drops the two functions and the column goes with
    -- them.
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
    p.obec,
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

-- ---------------------------------------------------------------------------
-- 5. pipeline_board_public -- migration 504's body, six columns REMOVED. The
--    board loads its cards whole and filters them in the browser (rule 22), so
--    it keeps the four chip codes; `place_search_text`/`locality`/`okres`/
--    `region`/`district`/`street` were mapped onto the card object and rendered
--    by nothing (lib/pipelineBoardModel).
-- ---------------------------------------------------------------------------

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

-- ---------------------------------------------------------------------------
-- 6. rebuild_properties_map_mv -- migration 440's body, ONE change: `district`
--    leaves the cover index's INCLUDE list, because the column it names is gone
--    from the projection the matview is built from and the statement would fail
--    at the next cron tick.
--
--    Nothing takes its place. The INCLUDE list was written when the map's
--    select list was eight columns; it has not covered MAP_COLS since (no
--    listing_id, source, source_id_native, price_per_m2, price_per_m2_basis),
--    so no index-only scan is being preserved or lost here -- adding
--    `display_label` would only widen every index tuple by a street address.
--    Everything else is verbatim, including the analyze-before-swap, the
--    anon-SELECT self-check and the pg_notify (all three are pinned:
--    tests/test_browse_read_path_guardrail.py, tests/test_browse_grant_drift.py).
-- ---------------------------------------------------------------------------

create or replace function rebuild_properties_map_mv()
returns void
language plpgsql
security definer
set search_path = public
as $fn$
declare
  t0 timestamptz := clock_timestamp();
  n  bigint;
begin
  if not pg_try_advisory_lock(hashtext('rebuild_properties_map_mv')) then
    raise notice 'rebuild_properties_map_mv: previous run still active, skipping tick';
    return;
  end if;
  begin
    execute 'drop materialized view if exists properties_map_mv_next';
    execute $q$
      create materialized view properties_map_mv_next as
      select * from browse_projection
      where lat is not null and lng is not null
      order by category_main, category_type, lat, lng
    $q$;
    execute 'create unique index properties_map_mv_next_pk on properties_map_mv_next (property_id)';
    execute $q$
      create index properties_map_mv_next_cover on properties_map_mv_next
        (category_main, category_type, lat, lng)
        include (sreality_id, price_czk, disposition, subtype, area_m2,
                 last_seen_at, first_seen_at, is_active)
    $q$;
    execute 'analyze properties_map_mv_next';
    execute 'select count(*) from properties_map_mv_next' into n;

    execute 'drop materialized view if exists properties_map_mv';
    execute 'alter materialized view properties_map_mv_next rename to properties_map_mv';
    execute 'alter index properties_map_mv_next_pk rename to properties_map_mv_pk';
    execute 'alter index properties_map_mv_next_cover rename to properties_map_mv_cover';
    execute 'grant select on properties_map_mv to authenticated';

    if has_table_privilege('anon', 'properties_map_mv', 'SELECT') then
      raise exception 'rebuild_properties_map_mv: anon must never hold SELECT on properties_map_mv -- refusing to publish this rebuild (see migration 374)';
    end if;

    update derived_artifacts
       set last_succeeded_at = now(),
           complete_through  = now(),
           last_duration_ms  = (extract(epoch from clock_timestamp() - t0) * 1000)::integer,
           last_rows         = n
     where name = 'properties_map_mv';
    perform pg_notify('pgrst', 'reload schema');
  exception when others then
    perform pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));
    raise;
  end;
  perform pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));
end
$fn$;

-- ---------------------------------------------------------------------------
-- 7. The base-table columns behind them.
--
--    `properties.place_search_text` is a GENERATED STORED column (migration
--    302) added so the chip predicate could be aliased at the `properties`
--    table directly, for the dedup Decision history and Queue -- surfaces the
--    2026-08 NEW DEDUP cutoff removed wholesale. No index, no view, no reader.
--
--    `home_city_id` / `home_city_computed_at` / `recompute_home_city()` are one
--    unit: the column, the "have I computed this row yet" marker that made the
--    job incremental, and the job. `properties_home_city_id_idx` drops with its
--    column. DROP COLUMN is catalog-only (Postgres marks the attribute dropped
--    and rewrites nothing), so this is fast on a 577k-row table -- but it is
--    also irreversible, which is why the header states the reader census rather
--    than asserting there is none.
--
--    Its OWN short transaction: `alter table properties drop column` takes
--    ACCESS EXCLUSIVE on a 577k-row hot table that every scrape write and both
--    rebuilds touch, so it must not sit inside the same long transaction as the
--    four view re-creates, and it must fail fast rather than queue.
-- ---------------------------------------------------------------------------

commit;

begin;

set local lock_timeout = '5s';

-- `home_city_id`'s ONE reader, and the SPA hatch that was its one caller goes
-- in the same PR. `listings_with_city_quality(jsonb,int,int,jsonb)` (live body:
-- migration 375) is the pre-W5 city-quality path: it walks `browse_list`, joins
-- `curated_cities_public` on `l.home_city_id` and returns a listing-id
-- allowlist. Migration 436 replaced it with `curated_cities_matching()` ->
-- `obec_id = any(...)`, ONE definition of "matches" (rule 16), and the only
-- thing still able to reach this one was `?cityQualityLegacy=1`. Dropping it
-- with the hatch is the bundle roadmap/hydration-sprint W7b already names --
-- leaving it would strand a function whose join column this file removes.
drop function if exists listings_with_city_quality(jsonb, int, int, jsonb);

alter table properties drop column if exists place_search_text;
alter table properties drop column if exists home_city_id;
alter table properties drop column if exists home_city_computed_at;

drop function if exists recompute_home_city(boolean);

-- Close the window in which a stale schema cache still advertises the dropped
-- columns to PostgREST (migrations 277/439/494/503 precedent).
select pg_notify('pgrst', 'reload schema');

commit;

-- ---------------------------------------------------------------------------
-- 8. The two read models, rebuilt NOW, in autocommit.
--
--    `properties_map_mv` does not exist at all until this runs (section 1
--    dropped it), and `browse_list` is eight columns WIDER than the view until
--    it does -- the silent-NULL hazard in the header. The */15 and 7,37 cron
--    ticks would close both within ~20 minutes on their own; closing them here
--    means the schema and the read models are never out of step.
--
--    statement_timeout is raised the way the two cron jobs raise it (migrations
--    413 / 371): the `postgres` role this file is applied as reports 2 min and
--    the rebuild averages 270 s, so without the raise it is cancelled every
--    time.
-- ---------------------------------------------------------------------------

set statement_timeout = '900s';
set lock_timeout = '30s';

select rebuild_browse_list();
select rebuild_properties_map_mv();

reset statement_timeout;
reset lock_timeout;

-- ---------------------------------------------------------------------------
-- 9. Post-rebuild assertions, in their own short transaction.
--
--    Both rebuilds SKIP (notice, not error) when a cron tick already holds
--    their advisory lock. For `browse_list` a skip is the dangerous case: the
--    table would keep its eight extra columns and the next merge would write
--    NULLs into them positionally. For `properties_map_mv` a skip leaves the
--    matview MISSING. Fail loudly on either.
-- ---------------------------------------------------------------------------

begin;

set local lock_timeout = '5s';

do $$
declare v_left text;
begin
  select string_agg(format('%s.%s', t, a), ', ') into v_left from (
    select 'browse_list' as t, a.attname as a
      from pg_attribute a
     where a.attrelid = 'public.browse_list'::regclass
       and not a.attisdropped and a.attnum > 0
       and a.attname in ('locality', 'district', 'street', 'obec', 'okres',
                         'region', 'place_search_text', 'home_city_id')
    union all
    select 'properties_map_mv', a.attname
      from pg_attribute a
     where a.attrelid = 'public.properties_map_mv'::regclass
       and not a.attisdropped and a.attnum > 0
       and a.attname in ('locality', 'district', 'street', 'obec', 'okres',
                         'region', 'place_search_text', 'home_city_id')
  ) s;
  if v_left is not null then
    raise exception
      'read model(s) still carry the dropped W3 columns (%) -- a concurrent '
      'rebuild held the advisory lock and this one skipped; pause the browse '
      'rebuild cron and re-run rebuild_browse_list() / '
      'rebuild_properties_map_mv()', v_left;
  end if;
end $$;

-- The narrowing must not have cost the read models a column they DO serve.
do $$
declare v_missing text;
begin
  select string_agg(t, ', ') into v_missing from (
    select 'browse_list' as t where not exists (
      select 1 from pg_attribute a
       where a.attrelid = 'public.browse_list'::regclass
         and a.attname = 'display_label' and not a.attisdropped)
    union all
    select 'properties_map_mv' where not exists (
      select 1 from pg_attribute a
       where a.attrelid = 'public.properties_map_mv'::regclass
         and a.attname = 'granularity_rank' and not a.attisdropped)
  ) s;
  if v_missing is not null then
    raise exception 'read model(s) % lost the W3 S1 columns', v_missing;
  end if;
end $$;

-- rebuild_properties_map_mv DROP+CREATEs properties_map_mv, which inherits the
-- default ACL; assert it did not re-grant MAINTAIN to a browser role (mirrors
-- migrations 342/343/363/425/503). MAINTAIN is PG17+, so skip on an older replay.
do $$
declare v_left integer;
begin
  if current_setting('server_version_num')::int < 170000 then
    return;
  end if;
  select count(*) into v_left
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'public' and c.relkind in ('r', 'm', 'p')
     and (has_table_privilege('authenticated', c.oid, 'MAINTAIN')
       or has_table_privilege('anon', c.oid, 'MAINTAIN'));
  if v_left > 0 then
    raise exception
      'browse rebuild re-granted MAINTAIN to a browser role on % relation(s)', v_left;
  end if;
end $$;

commit;
