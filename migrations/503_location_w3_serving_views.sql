-- 503_location_w3_serving_views.sql
--
-- Location simplification sprint, wave W3, step S1: the SERVING VIEWS read the
-- one answer table. ADDITIVE ONLY — every statement below is a
-- `create or replace view` that APPENDS columns, plus one new pure function.
-- Nothing is dropped here (the legacy place-text columns go in S4, once no
-- reader remains).
--
-- WHAT CHANGES, IN ONE LINE: Browse, the map, the kanban, the detail page, the
-- extension and the notification composer stop composing a place string out of
-- five legacy columns each, and read ONE `display_label` plus ONE set of RÚIAN
-- codes, both sourced from `listing_location` (migration 501).
--
-- THE JOIN. Property-grain surfaces join on `properties.repr_listing_ref_id` —
-- the property's DISPLAY listing, the same winner that already supplies price
-- and area (decision W3-1). Until today a property's place text could come from
-- a DIFFERENT listing than its price: `scripts/recompute_property_stats.py`
-- picks three separate winners (`repr` for price/area, `best_geo` for the admin
-- columns, `best_street` for the street), so a merged property could show
-- portal A's price over portal B's street. One join key collapses all three.
-- Listing-grain surfaces (`listing_feed_public`, `listings_public`,
-- `broker_listings_public`) join on `listings.id` directly.
--
-- WHY THE LABEL IS A FUNCTION AND NOT A STORED COLUMN. Migration 501 §"What is
-- deliberately NOT here" keeps the label out of `listing_location`: a stored
-- derivation is a second definition. But a label pasted into six view bodies is
-- six definitions, which is the same failure with more copies. So there is ONE
-- immutable SQL function, `location_display_label`, and every view calls it with
-- the same seven columns. It is `language sql` + `immutable` so the planner
-- inlines it — the 577k-row `browse_list` CTAS pays a expression evaluation, not
-- a function call.
--
-- RE-SOURCED, NOT RENAMED. `obec_id` / `okres_id` / `region_id` keep their names
-- and their types and now come from `ll.obec_kod` / `ll.okres_kod` /
-- `ll.kraj_kod`; `lat` / `lng` now come from `ST_Y/ST_X(ll.geom)`. These are
-- VALUE-IDENTICAL substitutions for a resolved row — `admin_boundaries.id` IS
-- the RÚIAN code (migrations 083/141, and the column comments on 162/171 say
-- so), so the district chips keep matching the same rows. For an UNRESOLVED row
-- they become NULL where the trigger-written legacy value used to be non-NULL,
-- which is why this migration must not be applied before rule 25's coverage
-- invariant is green: `count(listing_location) = count(listings where
-- is_active)` and `location_town_coverage`'s `cz_no_town` arm back at or below
-- its pre-W2 level (`verify_pipeline`). A row with no `listing_location` row, or
-- with a NULL `geom`, drops out of `properties_map_mv` (which filters
-- `lat is not null`) instead of showing a pin the resolver would not stand
-- behind — that is the intended posture, not a regression to paper over.
--
-- APPEND-ONLY IS ENFORCED BY POSTGRES, not by care: `create or replace view`
-- refuses to reposition, rename or retype an existing output column, so the
-- four new columns can only land at the end. That matters more than usual here
-- because `toolkit/browse_read_model.sync_browse_list` patches `browse_list`
-- with a POSITIONAL `INSERT INTO browse_list SELECT * FROM browse_projection`:
-- a column computed in the rebuild CTAS instead of in the view would make the
-- view and the table disagree in width and the patch would silently write NULLs.
-- Everything new is therefore IN THE VIEW.
--
-- NO WINDOW FUNCTION. The shared-pin count (`count(*) over (partition by geom)`)
-- that migration 501's header and the serving-contract doc both parked for W3 is
-- NOT computed (decision W3-2): `sync_browse_list`'s `WHERE property_id = ANY(…)`
-- cannot be pushed below a window function, so every merge would aggregate the
-- whole corpus. It has no producer and no measured need — the circle rule is
-- granularity-only.
--
-- COST. Two added left joins per serving view: one on `listing_location`'s
-- PRIMARY KEY (~756k rows) and one on the 11-row `location_granularity_rank`
-- lookup. No new index — no filter reads the new columns yet (S3 flips the chip
-- predicates and will bring its own measurement). `rebuild_browse_list` averages
-- 270 s against a 600 s statement_timeout (migration 413), so the first cron
-- tick after this lands is the measurement that matters; `derived_artifacts.
-- last_duration_ms` records it.
--
-- TRANSACTION SHAPE mirrors migration 425: view DDL in one short transaction
-- under a 5 s lock_timeout, then the two read-model rebuilds in AUTOCOMMIT (a
-- 270 s + 170 s pair inside one transaction would hold `browse_list`'s ACCESS
-- EXCLUSIVE across both, and every browser read of Browse would ERROR on
-- `authenticated`'s 8 s statement_timeout rather than merely queue), then the
-- assertions. The apply path re-runs the WHOLE file on a lock timeout, and every
-- statement here is idempotent.
--
-- TWO GUARDS, AND WHAT EACH FAILURE MEANS FOR THE DEPLOY.
--
--   * SECTION 0 runs BEFORE any DDL: it compares today's `properties_map_mv`
--     row count with the count the new definition WOULD produce, and aborts the
--     whole file if the map would lose more than 5 % of its pins. A pin collapse
--     is what an incomplete `listing_location` looks like from the map's side,
--     and it must fail loudly here rather than ship a half-empty map. Nothing has
--     been applied when it fires — fix the coverage, then re-run.
--
--   * SECTION 9 runs AFTER the forced rebuilds and asserts both read models are
--     actually WIDE. Both rebuilds SKIP (notice, not error) when a cron tick
--     already holds their advisory lock, and a skip leaves this file aborting
--     with **the views wide and the read models narrow** — a state in which
--     `select=display_label` against browse_list / properties_map_mv is a
--     PostgREST 400. That is recoverable and not corrupting: the next */15 and
--     7,37 cron ticks rebuild both, or re-run this file. **Never merge the SPA
--     until both read models are confirmed wide** (section 9 passing IS that
--     confirmation).

begin;

set local lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 0. PIN-COLLAPSE GUARD -- runs before a single line of DDL.
--
--    `lat`/`lng` stop coming from the `properties_set_latlng` trigger and start
--    coming from `ST_Y/ST_X(ll.geom)`, and `properties_map_mv` is defined as
--    `where lat is not null`. So a property whose display listing has no
--    `listing_location` row, or whose row has a NULL geom, LOSES ITS PIN. That
--    is the correct posture for a resolved corpus (no pin the resolver will not
--    stand behind) and a silent disaster for an unresolved one.
--
--    Both numbers are measured HERE, before the replace, so this is a pure
--    prediction and an abort costs nothing: the transaction rolls back with the
--    schema untouched. 5 % is the tolerance, not 0 %, because the legacy pin
--    includes coordinates the resolver deliberately refuses (a Mapy-class
--    licence, migration 501's claim projection) — a small honest loss is the
--    point of the wave; a large one means the coverage gate was not green.
--
--    FOREIGN ROWS ARE OUT OF BOTH COUNTS. 2026-09-12: 19,275 idnes rows the
--    resolver determined FOREIGN never had an admissible portal pin — their legacy
--    pins were Mapy geocodes, removed by doctrine, not coverage lost — and 4,374
--    bazos town-less rows are dead ads whose latest page is the category index;
--    the Czech-side loss this guard measures is ~1 %. Counting the foreign pins
--    on the BEFORE side alone would read as a collapse the coverage gate has
--    already cleared.
-- ---------------------------------------------------------------------------

do $guard$
declare
  v_before bigint;
  v_after  bigint;
  v_lost   numeric;
begin
  if to_regclass('public.properties_map_mv') is null then
    raise notice 'properties_map_mv absent (fresh replay); pin guard skipped';
    return;
  end if;

  execute $q$
    select count(*)
      from properties_map_mv m
      join properties p on p.id = m.property_id
      left join listing_location ll on ll.listing_id = p.repr_listing_ref_id
     where ll.country_status is distinct from 'foreign'
  $q$ into v_before;

  select count(*) into v_after
    from properties p
    left join listing_location ll on ll.listing_id = p.repr_listing_ref_id
   where p.status = 'active'
     and ll.geom is not null
     and ll.country_status is distinct from 'foreign';

  raise notice 'W3 pin guard: properties_map_mv has % pins today, % after the swap',
    v_before, v_after;

  if v_before > 0 and v_after < (v_before * 0.95) then
    -- plpgsql RAISE takes bare `%` placeholders, never printf conversions, so the
    -- percentage is rounded here rather than formatted in the message.
    v_lost := round(100.0 * (v_before - v_after) / v_before, 1);
    raise exception
      'W3 pin collapse: the map would drop from % pins to % -- % %% lost, cap 5 %%. '
      'This migration is gated on rule 25 coverage: every ACTIVE listing has a '
      'listing_location row, and the display listing of every active property is '
      'among them. Check verify_pipeline''s location_town_coverage and the count '
      'of active properties whose repr_listing_ref_id has no row, then re-run. '
      'Nothing has been applied.',
      v_before, v_after, v_lost;
  end if;
end
$guard$;

-- ---------------------------------------------------------------------------
-- 1. location_display_label -- the ONE definition of a place string.
--
--    The fallback order is decision W3-2, and it is deliberately short:
--
--      1. foreign  -> the country code. `country_status` is a DETERMINATION the
--         resolver makes (501), never a default for "no town found", so this
--         branch fires only when the resolver actually decided the listing is
--         outside CZ. It is FIRST because a foreign row has no RÚIAN chain to
--         fall through to.
--      2. street   -> "Street cp/co, Obec". The house number renders as the
--         Czech "popisné/orientační" pair when both are known ("Radkovská 262/8")
--         and as whichever one exists otherwise; a street with neither number is
--         still a street.
--      3. cast_obce-> "Část obce, Obec", but ONLY when the part says something
--         the town does not. "Brno, Brno" is noise.
--      4. town     -> "Obec".
--      5. nothing  -> NULL. A CZ row with no town is the red line rule 25
--         measures; it must render as "—", never as a bare "CZ".
--
--    Administrative names are always RÚIAN's own spelling (501), so this
--    function never sees a portal's free text and there is nothing to
--    de-duplicate or second-guess. `nullif(btrim(x), '')` throughout because a
--    whitespace-only name must behave exactly like a NULL one.
-- ---------------------------------------------------------------------------

create or replace function location_display_label(
  p_street_name     text,
  p_house_number_cp text,
  p_house_number_co text,
  p_obec_name       text,
  p_cast_obce_name  text,
  p_country_code    text,
  p_country_status  country_status
) returns text
language sql
immutable
parallel safe
as $fn$
  select case
    when p_country_status = 'foreign'
      then nullif(btrim(p_country_code), '')
    when nullif(btrim(p_street_name), '') is not null
      then nullif(
             concat_ws(', ',
               concat_ws(' ',
                 btrim(p_street_name),
                 nullif(concat_ws('/',
                   nullif(btrim(p_house_number_cp), ''),
                   nullif(btrim(p_house_number_co), '')), '')),
               nullif(btrim(p_obec_name), '')),
             '')
    when nullif(btrim(p_cast_obce_name), '') is not null
     and nullif(btrim(p_obec_name), '') is not null
     and btrim(p_cast_obce_name) <> btrim(p_obec_name)
      then btrim(p_cast_obce_name) || ', ' || btrim(p_obec_name)
    else nullif(btrim(p_obec_name), '')
  end
$fn$;

comment on function location_display_label(text, text, text, text, text, text, country_status) is
  'The ONE place string. Called by browse_projection, listing_feed_public, '
  'properties_public, listings_public and broker_listings_public with the seven '
  'listing_location columns, and by the API (portal_lookup, notification_outbox, '
  'notifications, curation) with the same seven. Fallback order: foreign country '
  'code -> street + house number + obec -> cast obce + obec -> obec -> NULL. '
  'Decision W3-2; docs/design/location-serving-contract.md.';

-- anon is dark (migration 299); `authenticated` needs EXECUTE because
-- pipeline_board_public and the other browser-facing views run it, and the
-- default ACL on a new function is PUBLIC, which would also reach anon.
revoke all on function location_display_label(text, text, text, text, text, text, country_status) from public, anon;
grant execute on function location_display_label(text, text, text, text, text, text, country_status) to authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 2. browse_projection -- migration 475's body, five columns RE-SOURCED and four
--    APPENDED. `browse_list` and `properties_map_mv` both materialize
--    `select * from browse_projection`, so they inherit all nine changes at
--    their next rebuild (forced at the foot of this file).
-- ---------------------------------------------------------------------------

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
    p.locality,
    p.district,
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
    p.street,
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
    concat_ws(', '::text, p.street, p.locality) as place_search_text,
    p.asset_id,
    p.repr_listing_ref_id as listing_id,
    (select l.source_id_native from listings l where l.id = p.repr_listing_ref_id) as source_id_native,
    p.all_sources,
    p.active_sources,
    p.home_city_id,
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

revoke all on browse_projection from anon;
grant select on browse_projection to authenticated;

-- ---------------------------------------------------------------------------
-- 3. listing_feed_public -- the portal-mirror lane (migration 475), same four
--    columns, same five re-sourced, joined on the listing itself. Without this
--    the single-portal / broker-scoped Browse lane would silently keep serving
--    legacy place text and legacy pins while the property lane moved.
-- ---------------------------------------------------------------------------

create or replace view listing_feed_public as
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
  concat_ws(', '::text, l.street, l.locality) as place_search_text,
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

revoke all on listing_feed_public from anon;
grant select on listing_feed_public to authenticated;

-- ---------------------------------------------------------------------------
-- 4. properties_public -- ONE appended column. The detail page, the watchdog
--    matcher and pipeline_board_public all read this view; S2 needs only the
--    label here. The codes and the pin stay on their legacy source until S3
--    flips the chip predicate and the watchdog's radius with one measurement —
--    re-sourcing them here today would move the watchdog's ST_DWithin under a
--    reader that has not been re-tested.
-- ---------------------------------------------------------------------------

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
    coalesce(p.street, l.street) as street,
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
    concat_ws(', '::text, p.street, p.locality) as place_search_text,
    p.asset_id,
    p.mf_reference_rent,
    p.published_at,
    p.repr_listing_ref_id as listing_id,
    l.source_id_native,
    p.home_city_id,
    measure_price_per_m2_basis(p.category_main, p.category_type) as price_per_m2_basis,
    -- ---- appended by migration 503 (W3 S1) ----
    location_display_label(ll.street_name, ll.house_number_cp, ll.house_number_co,
                           ll.obec_name, ll.cast_obce_name, ll.country_code,
                           ll.country_status) as display_label
from properties p
     left join listings l on l.id = p.repr_listing_ref_id
     left join listing_location ll on ll.listing_id = p.repr_listing_ref_id
where p.status = 'active'::text;

revoke all on properties_public from anon;
grant select on properties_public to authenticated;

-- ---------------------------------------------------------------------------
-- 5. listings_public -- the detail page + every comparable read. `geom` is the
--    ONE name `listings` and `listing_location` share, so the two references to
--    it are qualified; everything else stays unqualified exactly as migration
--    494 left it.
-- ---------------------------------------------------------------------------

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
  locality,
  district,
  locality_district_id,
  locality_region_id,
  st_y(listings.geom::geometry) as lat,
  st_x(listings.geom::geometry) as lng,
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
  street,
  house_number,
  mf_reference_rent_czk,
  mf_gross_yield_pct,
  mf_reference_rent,
  obec,
  okres,
  region,
  subtype,
  obec_id,
  okres_id,
  region_id,
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

revoke all on listings_public from anon;
grant select on listings_public to authenticated;

-- ---------------------------------------------------------------------------
-- 6. pipeline_board_public -- migration 425's body plus the label, taken from
--    properties_public rather than from listing_location directly. This view is
--    `security_invoker = true` (it inherits its tenant scoping from
--    property_pipeline_public), and `listing_location` is revoked from
--    `authenticated` (migration 501) — a direct join here would fail with
--    "permission denied" for every browser read. properties_public is an
--    owner-rights view, so the label arrives already computed.
-- ---------------------------------------------------------------------------

create or replace view pipeline_board_public
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
  p.street,
  p.district,
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
  p.place_search_text,
  p.obec,
  p.locality,
  p.okres,
  p.region,
  p.is_active,
  p.category_type,
  p.price_per_m2,
  p.price_per_m2_basis,
  -- ---- appended by migration 503 (W3 S1) ----
  p.display_label
from property_pipeline_public pp
left join properties_public p on p.property_id = pp.property_id;

revoke all on pipeline_board_public from public, anon;
grant select on pipeline_board_public to authenticated;

-- ---------------------------------------------------------------------------
-- 7. broker_listings_public -- migration 358's body plus the label. The broker
--    inventory table is one of the eleven place-rendering sites; without this it
--    would be the only surface still assembling `locality ?? district` by hand.
-- ---------------------------------------------------------------------------

create or replace view broker_listings_public as
select
  bi.broker_id,
  l.sreality_id,
  l.source,
  l.source_url,
  l.locality,
  l.district,
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

-- Amendment A6: the broker directory stays dark to browser roles (migration 299
-- part F revoked 224's anon grant); the SPA reads it only through the
-- PII-masking /brokers API. Re-state, never re-grant.
revoke all on broker_listings_public from anon, authenticated;

-- Close the window in which a fresh `select=display_label` 400s on a stale
-- schema cache (migrations 277/439/494 precedent).
select pg_notify('pgrst', 'reload schema');

commit;

-- ---------------------------------------------------------------------------
-- 8. The two read models, rebuilt NOW, in autocommit.
--
--    `browse_list` and `properties_map_mv` are materializations of
--    browse_projection: until they are rebuilt they are four columns narrower
--    than the view, and the first SPA read of `select=display_label` gets a
--    PostgREST 400. The */15 and 7,37 cron ticks would close that window on
--    their own within ~20 minutes; closing it here instead means the schema and
--    the read models are never out of step.
--
--    statement_timeout is raised the way the two cron jobs raise it (migration
--    413 / 371). The `postgres` role this file is applied as reports 2 min;
--    the rebuild averages 270 s, so without the raise it is cancelled every
--    time. 900 s leaves headroom over the 600 s the cron uses, and over the
--    added join.
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
--    Both rebuilds SKIP (notice, not error) when a cron tick already holds their
--    advisory lock. A skip would leave the read models four columns short of the
--    view, and the only symptom would be a 400 from PostgREST the first time a
--    consumer asks for display_label. Fail loudly instead.
-- ---------------------------------------------------------------------------

begin;

set local lock_timeout = '5s';

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
    raise exception
      'read model(s) % lack the W3 columns -- a concurrent rebuild held the '
      'advisory lock and this one skipped; pause the browse rebuild cron and '
      'retry migration 503', v_missing;
  end if;
end $$;

-- rebuild_properties_map_mv DROP+CREATEs properties_map_mv, which inherits the
-- default ACL; assert it did not re-grant MAINTAIN to a browser role (mirrors
-- migrations 342/343/363/425). MAINTAIN is PG17+, so skip on an older replay.
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
