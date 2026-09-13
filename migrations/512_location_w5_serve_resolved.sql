-- 512_location_w5_serve_resolved.sql
--
-- W5 of the location simplification sprint. THE CONSUMER RULE, operator ruling
-- 2026-09-13: "any consumer will only work with those listings not within the
-- exempted set, and any of these in the exempted set will become visible to
-- consumers (browse, dedup, notifications etc) only after they are properly
-- processed."
--
-- A listing is SERVED only when `listing_location` has an ANSWER for it -- a
-- point, or the determination that it is abroad. That is the whole rule, and it
-- is deliberately NOT a flag and NOT a new column: it reads the one store
-- directly, so it covers the 36,981 rows of the migration-510 audit set, every
-- future listing whose page carries no location at all, and nothing else. A row
-- comes back the moment the lane resolves it -- there is no backfill, nothing to
-- re-stamp, and no second place to remember to update.
--
-- ONE DEFINITION. The rule is `location_data.claims_common.SERVED_LOCATION_PREDICATE`,
-- rendered here onto each surface's own listing-id expression. The rendered text is
-- pinned character-for-character against the Python constant by
-- tests/test_location_w5_serve_resolved.py, so the two cannot drift.
--
-- WHERE IT LANDS, AND WHERE IT DELIBERATELY DOES NOT.
--   * `browse_projection` -- the ONE projection Browse, Stats and the map all
--     materialize from (`browse_list` via rebuild_browse_list()/sync_browse_list,
--     `properties_map_mv` via rebuild_properties_map_mv()). Keyed on the property's
--     DISPLAY listing, `repr_listing_ref_id`, because that is the row a Browse card
--     renders. Measured 2026-09-13: 44,702 of 711,600 Browse rows fail the rule
--     (2,953 of them still-live ads, 37,173 currently drawing a map pin).
--   * `listing_feed_public` -- the listing-grain LIST surface, keyed on its own
--     `l.id`.
--   * NOT `listings_public`, NOT `properties_public`, NOT `pipeline_board_public`.
--     Those are DETAIL-by-id surfaces (the listing page, the extension, the
--     watchdog's own match relation, the operator's kanban cards). A direct link
--     to an unresolved listing must keep working, and a pipeline card the operator
--     created must never vanish from the board -- rule 22 makes it operator state.
--     The audit page's links into the exempted set are exactly this: detail reads.
--   * NOT the broker surfaces. `broker_region_type_stats` already requires
--     `obec_kod IS NOT NULL`, which is strictly narrower than this rule, and the
--     broker inventory is attribution, not a consumer feed.
--
-- The code side of the wave carries the same constant into the watchdog matcher
-- (which reads `properties_public`, so it cannot inherit this) and into path C
-- candidate generation. `toolkit/comparables._shared_filter_where` needs no change:
-- its first clause is `ll.geom IS NOT NULL`, which is this rule minus the foreign
-- branch, i.e. strictly narrower (a comparable must have a point regardless).
--
-- APPLY BEFORE THE MERGE. This only ever HIDES rows, so a database carrying it
-- while the old code runs is correct; the reverse (code that assumes the rule with
-- a database that has not got it) is the one that would leak rows into the
-- watchdog. Nothing here is destructive: two `create or replace` bodies that
-- change one WHERE each, and two rebuilds of relations that are caches.
--
-- ORDER. This is 508's body plus a WHERE, so it applies AFTER 506 and 508 -- against
-- an older, wider `browse_projection` a `create or replace view` is refused outright
-- ("cannot drop columns from view"), which is the right failure. Apply through
-- `apply_migration.yml`, not the MCP: section 3 runs two multi-minute rebuilds, and
-- the MCP wraps a payload in one transaction that would hold their swap locks throughout.
--
-- COST. Measured on production 2026-09-13: the planner turns the correlated EXISTS into
-- a HASH JOIN against `listing_location`'s primary key (it is unique, so the semi-join
-- degenerates to a join) -- one more hash of a relation the projection already scans,
-- not a per-row subplan.
--
-- IDEMPOTENT + STATEMENT AUTOCOMMIT. No begin/commit, because the apply workflow
-- retries a lock_timeout from statement 1 and a partial apply must be resumable.
-- `create or replace view` cannot reposition or retype a column, so re-running is
-- a no-op; both rebuilds are blue-green and self-skipping under their advisory lock.

-- A `create or replace view` needs ACCESS EXCLUSIVE on the view, and both rebuild
-- functions hold locks on `browse_projection` for minutes at a time. Fail fast
-- instead of queueing in front of every reader (the hot-table DDL rule): if this
-- aborts, wait for the gap between `*/15` ticks and re-run.
set lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 1. browse_projection -- migration 508's body, VERBATIM, one clause appended.
--    `browse_list` and `properties_map_mv` both materialize `select * from
--    browse_projection`, so both inherit the rule at the rebuilds forced below;
--    `sync_browse_list`'s DELETE + positional re-INSERT means a merge survivor
--    whose display listing is unresolved simply does not reappear.
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
where p.status = 'active'::text
  -- THE CONSUMER RULE == location_data.claims_common.SERVED_LOCATION_PREDICATE,
  --    rendered on the DISPLAY listing. Pinned by tests/test_location_w5_serve_resolved.py.
  and EXISTS (SELECT 1 FROM listing_location sl WHERE sl.listing_id = p.repr_listing_ref_id AND (sl.geom IS NOT NULL OR sl.country_status = 'foreign'));

revoke all on browse_projection from anon, authenticated;
grant select on browse_projection to authenticated;

-- ---------------------------------------------------------------------------
-- 2. listing_feed_public -- migration 508's body, VERBATIM, one clause appended.
--    A WHERE is the only change a LIST surface needs: the column list is
--    untouched, so every PostgREST select against it keeps compiling.
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
where p.status = 'active'
  -- THE CONSUMER RULE == location_data.claims_common.SERVED_LOCATION_PREDICATE,
  --    rendered on the FEED's own listing. Pinned by tests/test_location_w5_serve_resolved.py.
  and EXISTS (SELECT 1 FROM listing_location sl WHERE sl.listing_id = l.id AND (sl.geom IS NOT NULL OR sl.country_status = 'foreign'));

revoke all on listing_feed_public from anon, authenticated;
grant select on listing_feed_public to authenticated;

-- ---------------------------------------------------------------------------
-- 3. The forced rebuilds. Both relations are materializations of the projection
--    above, so until each is rebuilt the rule is live in the view and absent from
--    what Browse and the map actually read. `lock_timeout` is reset first: the
--    blue-green swap takes ACCESS EXCLUSIVE on `browse_list` /
--    `properties_map_mv` at the END of a several-minute build, and killing it at
--    5 s would throw away the whole build.
-- ---------------------------------------------------------------------------

reset lock_timeout;
set statement_timeout = '900s';

select rebuild_browse_list();
select rebuild_properties_map_mv();

reset statement_timeout;

-- ---------------------------------------------------------------------------
-- 4. The proof. `pg_get_viewdef` re-prints the parsed tree rather than the text
--    this file sent, so the assert is on the SEMANTIC pieces of the rule (the
--    correlated `listing_location sl` probe and both arms of its OR) and not on a
--    string match -- a character pin belongs offline, and that is what
--    tests/test_location_w5_serve_resolved.py is. The two rebuild functions are
--    asserted to still read `browse_projection`, which is the whole reason
--    `browse_list` and `properties_map_mv` inherit the rule rather than restating it.
-- ---------------------------------------------------------------------------

do $$
declare
  v     text;
  rel   text;
  stale bigint;
begin
  foreach rel in array array['browse_projection', 'listing_feed_public'] loop
    v := lower(regexp_replace(pg_get_viewdef(rel::regclass, true), '\s+', ' ', 'g'));
    if position('listing_location sl' in v) = 0
       or position('sl.geom is not null' in v) = 0
       or position('''foreign''' in v) = 0 then
      raise exception '512: % does not carry the consumer rule (SERVED_LOCATION_PREDICATE)', rel;
    end if;
  end loop;

  foreach rel in array array['rebuild_browse_list', 'rebuild_properties_map_mv'] loop
    if position('browse_projection' in pg_get_functiondef(rel::regproc)) = 0 then
      raise exception '512: %() no longer reads browse_projection -- the rule would not be inherited', rel;
    end if;
  end loop;

  -- Reported, never asserted: a rebuild self-skips when the `*/15` cron tick already
  -- holds its advisory lock, and that is a re-run, not a failed migration.
  execute $q$
    select count(*) from browse_list b
     where not exists (select 1 from listing_location sl
                        where sl.listing_id = b.listing_id
                          and (sl.geom is not null or sl.country_status = 'foreign'))
  $q$ into stale;
  if stale > 0 then
    raise notice '512: browse_list still carries % unresolved rows -- rebuild_browse_list() skipped a tick, re-run this file', stale;
  else
    raise notice '512: browse_list carries no unresolved rows';
  end if;
end
$$;
