-- 371_property_source_arrays.sql
--
-- Make Browse's "Portal" filter mean what the control says: "this property is
-- listed on that portal". Today it means "this property's REPRESENTATIVE listing
-- happens to be from that portal", and the difference is not cosmetic.
--
-- THE BUG. `properties.source` is written by the `repr` CTE in
-- scripts/recompute_property_stats.py:207-227, which orders
-- `is_active DESC, source_trust_rank(source), last_seen_at DESC`. Exactly one
-- child wins, and sreality is trust rank 1 (migration 311). So a property with a
-- live idnes listing AND a sreality sibling gets source='sreality' and vanishes
-- from `portal = idnes`. Measured live 2026-08-05, per portal (properties hidden
-- from their own portal's filter / properties that genuinely have an active
-- listing there): idnes 23,229/105,050 (22.1%), maxima 71/264 (26.9%),
-- realitymix 9,029/45,990 (19.6%), ceskereality 11,799/61,617 (19.1%),
-- remax 1,017/7,492 (13.6%), mmreality 1,336/10,007 (13.4%), bezrealitky
-- 630/5,304 (11.9%), bazos 2,887/26,916 (10.7%), sreality 0/96,020 (0.0%).
-- 30,899 distinct properties are affected; 97% of every hidden row is sreality
-- winning the trust rank, so the error is ~zero when a selection INCLUDES
-- sreality and worst when it excludes it.
--
-- The error is ONE-DIRECTIONAL — omission only. Across all nine portals just 59
-- rows appear under a portal where no active listing exists. Nothing wrong ever
-- shows up on screen; correct rows are simply absent, which is precisely the
-- failure an operator cannot detect by looking.
--
-- WORSE, IT BREAKS SET MONOTONICITY. Since PR #948 a single selected portal
-- reads the listing-grain `listing_feed_public` while >=2 portals read
-- property-grain `browse_list`, so ADDING a portal can SHRINK the result: 25 of
-- 36 portal pairs (69%) return fewer rows under Browse's default status filter.
-- Worst measured: idnes alone 107,225 -> tick Maxima (265 listings in total) ->
-- 82,027, down 23.5%.
--
-- RULE #16 IS ALREADY VIOLATED BY THIS, IN THE OTHER DIRECTION. The watchdog
-- matcher (api/notifications.py:404-406) and the shared toolkit matcher
-- (toolkit/comparables.py:393-395) BOTH apply the portal filter as
-- `l.source = ANY(...)` while walking `listings` — i.e. they already implement
-- "has a listing on that portal". Browse is the one surface that diverges, so a
-- saved filter can list one cohort and fire alerts on another. This migration
-- brings Browse INTO lockstep; it does not put it at risk.
--
-- WHY TWO STORED COLUMNS RATHER THAN A JOIN AT READ TIME.
-- The obvious shapes were measured and rejected:
--   * A grouped `LEFT JOIN (… GROUP BY property_id)` inside `browse_projection`
--     puts the aggregate on the nullable side of an outer join, so Postgres
--     cannot push `property_id = ANY(...)` through it. That matters because
--     toolkit/browse_read_model.py's `sync_browse_list` re-materializes single
--     properties inside EVERY merge transaction — ~1,451 merges/day — and would
--     have paid a full-table aggregate each time.
--     Measured, not assumed: the scoped shape timed out outright at 40 rows.
--   * A `LEFT JOIN LATERAL` does push down correctly (measured: 3 ms for 40
--     properties, index scan on listings_property_id_idx), but the same shape
--     over the FULL projection — which `rebuild_browse_list` runs every 5
--     minutes — did not finish inside a 60 s probe. Good for the scoped path,
--     unusable for the rebuild.
--   * Precomputing the aggregate into a matview refreshed by the rebuild would
--     fix both reads, but building it needs one full pass over `listings`, and
--     `listings` is 8.3 GB — that pass alone did not finish in 60 s. Anything
--     that rescans `listings` on the 5-minute rebuild cadence is out.
-- Storing the arrays on `properties` costs nothing at either read: the rebuild
-- and the merge patch both just copy two more columns. The work moves to
-- property maintenance, which is where every other child-derived precompute
-- already lives (mf_gross_yield_pct, near_*, last_change_at, distinct_site_count)
-- and which is dirty-set incremental by rule #20.
--
-- Both columns are NULLABLE with no default, so this is a catalog-only ALTER on
-- a 379 MB / 35-index table — no rewrite, no lock beyond the catalog update.
--
-- TWO columns, not one: 10,708 properties currently hold a live listing on one
-- portal and only a DEAD listing on another. Browse's status filter must select
-- between them, exactly the way queries.ts already picks a price-change-count
-- column from the selected window.

alter table properties add column all_sources text[];
alter table properties add column active_sources text[];

comment on column properties.all_sources is
  'Distinct `listings.source` over ALL children, any lifecycle. Maintained by '
  'scripts/recompute_property_stats.py (child_agg for the batch/dirty/single paths, '
  '_ATTACH_INSERT_SQL for fresh singletons). Browse filters `portal` against this '
  'when the status filter is not active-only. NULL only for a childless property.';

comment on column properties.active_sources is
  'Distinct `listings.source` over children with is_active. NULL when the property '
  'has no live listing (array_agg FILTER yields NULL, and NULL && anything is NULL, '
  'so such a property correctly drops out of an active-only portal filter). Same '
  'maintainers as all_sources.';

-- ---------------------------------------------------------------------------
-- BACKFILL — do NOT hand-roll one. Run the existing full property recompute:
--
--   gh workflow run recompute_property_stats.yml
--
-- The columns are populated by the same `child_agg` CTE that already computes
-- distinct_site_count, so a full sweep fills every property using the tested,
-- batched machinery on its own connection. This must finish BEFORE the frontend
-- that reads these columns is deployed, or portal-filtered Browse returns empty.
--
-- A hand-written batched UPDATE was tried and rejected: at ~2 ms/row it is ~20
-- minutes of sustained writes on a 35-index table. Worse, an obvious-looking
-- `create index … where all_sources is null` to drive the batches makes it
-- FOUR times slower — flipping the column moves the row in that partial index,
-- which disqualifies the HOT-update path and forces all 35 indexes to be
-- rewritten per row (measured: 26.6 s per 2,000 rows with the index, 9.8 s per
-- 5,000 without). Hence no such index here, deliberately.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- browse_projection: append the two columns. Both read models materialize
-- `select * from browse_projection`, so browse_list and properties_map_mv pick
-- them up from the rebuilds at the bottom of this file with no function change.
-- Column list is migration 363's verbatim, with the two additions at the end —
-- CREATE OR REPLACE VIEW can only append, so any accidental edit above fails loudly.
-- ---------------------------------------------------------------------------
create or replace view browse_projection as
select
    id as property_id,
    repr_listing_id as sreality_id,
    first_seen_at,
    last_seen_at,
    is_active,
    category_main,
    category_type,
    current_price_czk as price_czk,
    area_m2,
    disposition,
    locality,
    district,
    locality_district_id,
    locality_region_id,
    lat,
    lng,
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
    case
        when is_active then greatest(0, floor(extract(epoch from now() - first_seen_at) / 86400::numeric)::integer)
        else greatest(0, floor(extract(epoch from last_seen_at - first_seen_at) / 86400::numeric)::integer)
    end as tom_days,
    case
        when area_m2 is not null and area_m2 > 0::numeric and current_price_czk is not null then round(current_price_czk::numeric / area_m2, 2)
        else null::numeric
    end as price_per_m2,
    building_condition_level,
    apartment_condition_level,
    source,
    street,
    mf_reference_rent_czk,
    mf_gross_yield_pct,
    obec,
    okres,
    region,
    home_obec_pop,
    near_pop_5km,
    near_pop_15km,
    near_jobs_5km,
    near_jobs_15km,
    near_youth_5km,
    near_youth_15km,
    near_overall_5km,
    near_overall_15km,
    subtype,
    last_change_at,
    obec_id,
    okres_id,
    region_id,
    price_change_count,
    price_change_count_30d,
    price_change_count_90d,
    price_change_count_365d,
    total_price_change_pct,
    concat_ws(', '::text, street, locality) as place_search_text,
    asset_id,
    repr_listing_ref_id as listing_id,
    (select l.source_id_native from listings l where l.id = p.repr_listing_ref_id) as source_id_native,
    all_sources,
    active_sources
from properties p
where status = 'active'::text
  and (not (select publication_gate_enabled()) or published_at is not null);

-- ---------------------------------------------------------------------------
-- browse_stats_properties: same predicate swap, so the Stats tab cannot report a
-- different cohort than the cards it sits next to.
--
-- The function is ~21 KB / 177 lines and has been amended by many migrations
-- (080 -> 146 -> 155 -> 182 -> 203 -> 223 -> 329 -> 341 -> 351 -> …). Re-typing
-- it here to change ONE predicate would risk silently reverting any of those, so
-- this rewrites the LIVE definition in place instead: read it with
-- pg_get_functiondef, substitute the single predicate, re-execute. That cannot
-- drift from whatever the current body is.
--
-- Guarded on both sides: missing function or missing predicate raises rather
-- than silently doing nothing. If a future migration rewords that predicate this
-- migration is already applied and inert, but the guard is what makes a REPLAY
-- fail loudly instead of quietly shipping the old semantics.
-- ---------------------------------------------------------------------------
do $$
declare
  v_def text;
  v_pat constant text := 'l.source = any(portal_filter)';
  v_rep constant text :=
    '(case when active_only_filter then l.active_sources else l.all_sources end) && portal_filter';
begin
  select pg_get_functiondef(p.oid) into v_def
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
   where n.nspname = 'public' and p.proname = 'browse_stats_properties';

  if v_def is null then
    raise exception '371: browse_stats_properties() not found — migration 080 must apply first';
  end if;
  if position(v_pat in v_def) = 0 then
    raise exception
      '371: portal predicate "%" not found in browse_stats_properties() — a later '
      'migration reworded it; update this migration rather than skipping the swap', v_pat;
  end if;

  execute replace(v_def, v_pat, v_rep);
end $$;

-- Rebuild both read models now (both do `select * from browse_projection`) so the
-- new columns land immediately and the positional-insert window is closed:
-- toolkit/browse_read_model.py's sync_browse_list does a POSITIONAL
-- `insert into browse_list select * from browse_projection`, which throws
-- "INSERT has more expressions than target columns" until browse_list has the
-- same shape (migration 363 hit exactly this).
select rebuild_browse_list();
select rebuild_properties_map_mv();

-- rebuild_properties_map_mv DROP+CREATEs properties_map_mv, which inherits the
-- default ACL; assert it did not re-grant MAINTAIN to a browser role (mirrors
-- migration 342/343/363's guard). MAINTAIN is PG17+, so skip on the PG15 CI replay.
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
