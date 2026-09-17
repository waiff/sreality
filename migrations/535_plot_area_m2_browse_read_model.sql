-- 535_plot_area_m2_browse_read_model.sql
--
-- W21 follow-up: publish the plot measure ON THE BROWSE READ MODEL, so the
-- Browse "Lot area" filter reads the ONE definition instead of a column.
--
-- ---------------------------------------------------------------------------
-- WHY THIS FILE EXISTS (the defect it fixes)
-- ---------------------------------------------------------------------------
-- Migration 534 named the measure and moved the API-side readers (comparables,
-- the watchdog matcher) onto it, and set `pg_column = NULL` on the registry's
-- `min/max_estate_area` because no single `listings` column answers them.
--
-- On the SPA that is not a documentation nicety, it is a SWITCH:
-- `frontend/src/lib/registryQueryBuilder.ts` turns every browse-agenda filter
-- into a PostgREST predicate and skips any filter whose `pg_column` is null
-- (`if (filter.pg_column == null) continue;`). So the Lot-area inputs went from
-- "emits `.gte(estate_area, X)` and silently drops land" to "emits NOTHING" --
-- a filter the UI still offers and the query no longer applies. Worse than the
-- bug it replaced, and green in CI, because the drift test skipped null
-- `pg_column` too. That also broke rule 16 the other way round: the watchdog
-- would have matched on the measure while Browse matched on nothing.
--
-- THE FIX IS THE SAME ONE DEFINITION, ONE LEVEL OUT: `plot_area_m2` becomes a
-- COLUMN of the read model, computed by `public.plot_area_m2(...)` (534) inside
-- the projection, and the registry points at it. The SPA re-spells nothing; the
-- auto-builder dispatches it like any other column-backed filter; and
-- `tests/test_browse_read_path_guardrail.py::test_frontend_read_contract_subset_of_projection`
-- -- which collects every browse filter's `pg_column` and demands the projection
-- publish it -- becomes the rail that keeps the two ends together.
--
-- ---------------------------------------------------------------------------
-- THE THREE RELATIONS BROWSE FILTERS AGAINST, and what each needs
-- ---------------------------------------------------------------------------
--   browse_list          property grain: cards, table, counts. An UNLOGGED TABLE
--                        rebuilt `*/15` as `create unlogged table browse_list_next
--                        as select * from browse_projection`, so the view change
--                        alone would leave the column missing (PostgREST 400) for
--                        up to 15 minutes -- AND would break `sync_browse_list`'s
--                        `INSERT INTO browse_list SELECT * FROM browse_projection`,
--                        which is POSITIONAL and would see 65 expressions for 64
--                        columns (best-effort, so merges would just stop being
--                        read-your-writes until the rebuild). Both windows are
--                        closed here: the column is ADDed and BACKFILLed in this
--                        file, at the same trailing position the view puts it.
--   properties_map_mv    property grain: the map pins. A matview of the same
--                        `select *`, rebuilt `7,37`; a matview cannot be ALTERed,
--                        so this file REBUILDS it (the same function pg_cron runs,
--                        ~5 min on 679k rows) rather than leaving the map to answer
--                        400 for up to 30 minutes.
--   listing_feed_public  listing grain: the portal-mirror lane (list AND map). A
--                        plain view -- replaced here, correct at COMMIT.
--
-- `listings_public` is deliberately NOT touched: no browse-agenda filter reads it
-- (the SPA reads it for listing DISPLAY), and adding a derived column to it would
-- be a fourth copy of the measure with no caller.
--
-- EXPECT THIS FILE TO TAKE ~6 MINUTES, almost all of it the map rebuild. It holds
-- both rebuild advisory locks for that time, which makes every pg_cron tick
-- self-skip in milliseconds rather than start a 5-minute rebuild.
--
-- ---------------------------------------------------------------------------
-- CORRECTIONS TO 534'S PROSE (append-only: stated here, never by editing 534)
-- ---------------------------------------------------------------------------
--   * The mmreality headline-loss count is 19 rows, not 18: 5 byt, 2 komercni,
--     11 dum and ONE pozemek (inactive) state neither `usableArea` nor
--     `parcelArea`, so the parser yields no headline for them.
--   * `totalArea = parcelArea + usableArea` is the DUM identity, not a universal
--     one. On komercni / ostatni `totalArea == usableArea` (the page states no
--     parcel), and on pozemek `totalArea == parcelArea` (a parcel has no
--     interior to add). The defect is the same either way -- `totalArea` is a
--     DERIVED figure and never the parcel -- but the arithmetic differs by
--     category and 534's header states it as though it did not.
--   * 534's header says writers must not fill `estate_area` for land, while
--     mmreality's new parser fills it from `parcelArea` on land rows. Both are
--     right under one rule, which 534 failed to spell: a writer fills
--     `estate_area` ONLY from a LABELLED parcel cell the page itself states
--     (sreality, bezrealitky, idnes, ceskereality, maxima and now mmreality all
--     do), and NEVER by synthesising one from `area_m2`. What is forbidden is
--     manufacturing the column for the four portals whose land pages carry no
--     parcel label -- that would duplicate the headline into a second column and
--     make `plot_area_m2` unnecessary by making the data lie.
--
-- ---------------------------------------------------------------------------
-- MECHANICS
-- ---------------------------------------------------------------------------
-- IDEMPOTENT + STATEMENT AUTOCOMMIT, like 514 and 522: no begin/commit, because
-- `apply_migration.yml` retries the whole file on a lock timeout and a partial
-- apply must be resumable. Every statement is `create or replace`, an
-- `add column if not exists`, an idempotent UPDATE, or a blue-green rebuild.
--
-- ADDITIVE. Both view bodies are 522's (browse_projection) and 514's
-- (listing_feed_public) VERBATIM -- confirmed against the live catalog by column
-- name AND ordinal position before this file was written -- with ONE column
-- APPENDED at the end of each select list. No existing column moves, so the
-- positional insert stays correct and `create or replace view` is legal.

-- ci-allow-dynamic: rebuild_properties_map_mv blue-greens its relation through
-- `EXECUTE`d DDL and has since migration 277; this file only CALLS it.

-- ---------------------------------------------------------------------------
-- 0. Take both rebuild locks FIRST (522's preamble, same keys, same reasoning):
--    while they are held every pg_cron tick self-skips in milliseconds instead of
--    starting a rebuild, which is what makes the DDL below uncontended. The
--    acquire QUEUES behind an in-flight rebuild rather than aborting.
-- ---------------------------------------------------------------------------
set statement_timeout = '900s';
set lock_timeout = 0;

select pg_advisory_lock(hashtext('rebuild_browse_list'));
select pg_advisory_lock(hashtext('rebuild_properties_map_mv'));

-- Fail fast on the DDL itself (the hot-table rule): the only remaining holder
-- would be a `sync_browse_list` read, and the workflow retries the file.
set lock_timeout = '30s';

-- ---------------------------------------------------------------------------
-- 1. browse_projection -- migration 522's body VERBATIM, one column appended.
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
    gr.rank::int as granularity_rank,
    -- ---- appended by migration 535 (W21) ----
    -- THE plot measure (534), computed HERE so every Browse lane filters ONE
    -- definition. `area_m2` is the parcel for land (rule 23), so `estate_area`
    -- alone drops a third of the active land inventory.
    public.plot_area_m2(p.category_main, p.area_m2::numeric,
                        p.estate_area::numeric) as plot_area_m2
from properties p
     left join listing_location ll on ll.listing_id = p.repr_listing_ref_id
     left join location_granularity_rank gr on gr.granularity = ll.granularity
where p.status = 'active'::text
  -- THE CONSUMER RULE (rule 25), unchanged in MEANING and re-spelled for the planner
  -- by W13. 514 rendered location_data.claims_common.SERVED_LOCATION_PREDICATE here as
  -- an EXISTS against a second alias `sl`; because `listing_location_pkey` is UNIQUE on
  -- `listing_id`, the `ll` LEFT JOIN two lines up already yields AT MOST ONE row for the
  -- very same key, so the answer can simply be read off it. Provably the same rows (a
  -- 40,000-property probe on production 2026-09-14 found 0 disagreements), one fewer
  -- 821k-row pass. RED by tests/test_browse_read_path_guardrail.py if either arm is lost.
  and (ll.geom IS NOT NULL OR ll.country_status = 'foreign');
revoke all on browse_projection from anon, authenticated;
grant select on browse_projection to authenticated;

-- ---------------------------------------------------------------------------
-- 2. listing_feed_public -- migration 514's body VERBATIM, one column appended.
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
  gr.rank::int as granularity_rank,
  -- ---- appended by migration 535 (W21) ----
  -- Same measure on the portal-mirror lane, so the listing-grain and
  -- property-grain halves of Browse cannot answer a plot filter differently.
  public.plot_area_m2(l.category_main, l.area_m2::numeric,
                      l.estate_area::numeric) as plot_area_m2
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
-- 3. browse_list gets the column NOW, not at the next rebuild. `add column`
--    without a default is catalog-only (instant, no rewrite) and lands the
--    column at the SAME trailing position the view puts it, which is what keeps
--    `sync_browse_list`'s positional INSERT valid. The backfill is then what
--    makes the filter answer correctly before the `*/15` tick -- without it the
--    column is all-NULL and the filter is the same silent no-op this file
--    exists to end.
-- ---------------------------------------------------------------------------
alter table browse_list add column if not exists plot_area_m2 numeric;

update browse_list
   set plot_area_m2 = public.plot_area_m2(category_main, area_m2, estate_area)
 where plot_area_m2 is distinct from
       public.plot_area_m2(category_main, area_m2, estate_area);

-- ---------------------------------------------------------------------------
-- 4. The map. A matview cannot be ALTERed, so the column arrives only by a
--    rebuild -- the same blue-green function pg_cron runs at 7,37. Called here
--    so the map never answers 400 on a filter the UI is already offering.
--    ~5 min / 679k rows; the ACCESS EXCLUSIVE is taken only at the swap.
--    `pg_try_advisory_xact_lock` inside it succeeds: advisory locks are
--    re-entrant within one session, and this session already holds the key.
-- ---------------------------------------------------------------------------
select public.rebuild_properties_map_mv();

-- VERIFICATION (run after apply):
--
--   -- All four Browse relations publish the measure. READ pg_attribute, NOT
--   -- information_schema.columns: the SQL standard has no MATERIALIZED VIEW, so
--   -- Postgres's information_schema omits `properties_map_mv` ENTIRELY and the
--   -- convenient spelling reports a missing column that is actually there.
--   select c.relname, c.relkind,
--          count(*) filter (where a.attname = 'plot_area_m2') as has_plot,
--          max(a.attnum) filter (where a.attname = 'plot_area_m2') as pos,
--          max(a.attnum) as ncols
--     from pg_class c
--     join pg_namespace n on n.oid = c.relnamespace and n.nspname = 'public'
--     join pg_attribute a on a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped
--    where c.relname in ('browse_projection', 'browse_list',
--                        'properties_map_mv', 'listing_feed_public')
--    group by 1, 2 order by 1;      -- expect has_plot = 1 and pos = ncols on all four
--
--   -- MEASURED after this file was applied (2026-09-17): browse_list 65/65,
--   -- browse_projection 65/65, listing_feed_public 70/70, properties_map_mv 65/65.
--
--   -- browse_list's column agrees with the projection that will replace it.
--   select count(*) from browse_list
--    where plot_area_m2 is distinct from
--          public.plot_area_m2(category_main, area_m2, estate_area);   -- expect 0
--
--   -- The land the estate_area reader dropped, now reachable from Browse.
--   select count(*) filter (where estate_area is not null) as via_column,
--          count(*) filter (where plot_area_m2 is not null) as via_measure
--     from browse_list where category_main = 'pozemek';
