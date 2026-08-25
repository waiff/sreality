-- 425_measure_price_per_m2.sql
--
-- W4, THE KEYSTONE of the per-m2 measure-unification program
-- (docs/design/ppm2-measure-unification.md). Strictly after W1 (423) and W3 (424).
--
-- NORTH STAR: one measure, one definition, one label. Before this file the
-- platform carried EIGHT hand-typed copies of `price / area` in SQL alone, over
-- three grains, in two rounding conventions, with four different validity-bound
-- sets and no basis label anywhere. After it there is ONE named measure --
-- `measure_price_per_m2(price, area, category_main, category_type)` -- and one
-- named label -- `measure_price_per_m2_basis(category_main, category_type)` --
-- and every live relation reads them instead of re-deriving the formula.
--
-- ---------------------------------------------------------------------------
-- WHY THE MEASURE RETURNS round(x, 2) -- DO NOT "UNIFY ON THE UNROUNDED FORM"
-- ---------------------------------------------------------------------------
-- migrations/200 documents the mechanism: the SPA's keyset cursor sends
-- `price_per_m2.eq.<float64>` as its equal-value tiebreaker at the page seam. An
-- ~18-digit unrounded numeric does not round-trip through a JS Number, the
-- equality never matches, and rows are SILENTLY SKIPPED between pages. 200
-- rounded `properties_public` for exactly this reason; `listings_public` (420)
-- and `listing_feed_public` (370) were never rounded, so that row-drop is LIVE
-- TODAY on the single-portal Browse lane, whose `effectiveSort` passes
-- `price_per_m2` straight through. Putting all four views on the rounded measure
-- closes it. Any future session that "unifies on the unrounded form" re-breaks
-- Browse pagination.
--
-- ---------------------------------------------------------------------------
-- WHY THE MEASURE CARRIES NO `SET search_path`
-- ---------------------------------------------------------------------------
-- A SET clause makes a SQL function un-inlinable: the planner can no longer fold
-- the body into the calling query, so `where measure_price_per_m2(...) >= x`
-- degrades from an ordinary expression filter into a per-row function call and
-- every index condition that used to push down stops pushing down. The
-- declaration style copied here is `source_trust_rank`'s (migration 311) -- SQL,
-- IMMUTABLE, PARALLEL SAFE, single expression, no SET -- which is the repo's own
-- proof that the shape inlines: EXPLAIN (VERBOSE) of a predicate on
-- source_trust_rank prints the expanded CASE, not a function call.
--
-- THE BODY IS WRITTEN FLAT ON PURPOSE, and it is NOT stylistic. The declaration
-- is necessary for inlining but not sufficient: the BODY has to be inlinable
-- too, and that was established empirically against this database rather than
-- assumed. EXPLAIN (VERBOSE) over `listings`, live:
--
--   source_trust_rank        CASE with a test expr, casts          -> INLINES
--   price_per_m2_source_id   flat CASE, AND, IS NOT NULL, `>`      -> INLINES
--   the geo-cell block key   CASE, param used 3x, round, concat    -> INLINES
--   the street block key     CASE, OR, concat, coalesce            -> INLINES
--   listing_geo_cell_key     CASE, OR, `IN (...)`, NESTED CASE     -> does NOT
--
-- (the two block-key helpers are the geo-cell and street ones in the
-- location-data schema. They are named indirectly on purpose: that schema's
-- structural-contract test suite builds its corpus by substring-matching
-- migration TEXT, so spelling either name here would pull this file into it and
-- hold it to REVOKE rules that strip `authenticated` off region_stats.)
--
-- listing_geo_cell_key is the outlier and the only constructs it has that none
-- of the four inliners do are `IN (...)` and a nested CASE. So this body uses
-- neither: one flat CASE, equality/comparison, AND / OR, round(). It is longer
-- than the obvious `IN ('prodej','drazba','podil')` spelling and that is the
-- price of a predicate that keeps pushing down. Re-run the EXPLAINs before
-- "tidying" it -- tests/test_measure_price_per_m2.py asserts the plan shows the
-- expanded CASE and not a function call, so a tidy-up fails there first.
--
-- ---------------------------------------------------------------------------
-- THE BASIS VOCABULARY AND THE PER-BASIS VALIDITY FLOORS
-- ---------------------------------------------------------------------------
-- Four tokens, resolved from (category_main, category_type) at READ time, per
-- the Option-A fork (charter 2.2): `area_m2` stays polymorphic -- floor area for
-- byt/dum/komercni, PLOT area for pozemek -- and the MEASURE, not the column,
-- carries the denominator's meaning.
--
--   rent_monthly_czk_m2  category_type = 'pronajem'          floor: price >= 1 000
--   land_capital_czk_m2  category_main = 'pozemek' and a     floor: none
--                        capital category_type
--   sale_capital_czk_m2  a capital category_type             floor: price >= 100 000
--   NULL                 anything else -> no measure, no label
--
-- The floors live HERE and not at the write boundary. W1 deliberately did not
-- put a price floor on ingest: destroying a scraped price to protect a derived
-- figure is backwards. `listings.price_czk` stays faithful to the source; only
-- the DERIVED per-m2 figure is withheld when its numerator is implausible for
-- its basis. This is what finally kills the unit-price masquerade at the point
-- of consumption -- a "136 Kc" commercial rental is a Kc/m2/month advert price
-- mis-parsed as a total, and 136 / 250 m2 = 0.54 Kc/m2 is not a number any
-- surface should render.
--
-- RESOLUTION ORDER IS RENT-FIRST, AND IT MATTERS. `pozemek` + `pronajem` (1 845
-- live active properties) is a MONTHLY figure; labelling it `land_capital_*`
-- because category_main won the race would put a rent under a capital label --
-- the exact confusion the program exists to end. Rent wins; the plot-area
-- denominator is still the plot, which the label's `_m2` does not contradict.
--
-- CAPITAL category_type IS AN ENUMERATED ALLOWLIST -- ('prodej','drazba',
-- 'podil') -- not "everything that is not pronajem". Live `category_type` has
-- FOUR values, not the two the charter assumed: `drazba` (auction, 2 894 active
-- properties) and `podil` (co-ownership share, 7 701) are real and are capital
-- transactions. Treating them as sale keeps their per-m2 figure alive; treating
-- an UNKNOWN future value as sale would be a silent guess, so anything outside
-- the allowlist resolves to NULL basis and NULL measure -- a visible gap, which
-- the charter argues is strictly better than a silent basis switch. A NULL
-- `category_type` likewise yields NULL: it is genuinely undecidable whether the
-- price is a capital sum or a monthly rent, and a Kc/m2 without a basis is the
-- thing the north star forbids.
--
-- LIVE IMPACT, measured on production 2026-08-24 over 466 207 active
-- properties: 2 895 (0.62%) lose their per-m2 figure -- 2 315 rent (the
-- masquerade: 2 092 of them komercni/pronajem), 395 sale, 185 basis-
-- undecidable, 0 land. `price_czk`, `area_m2` and every snapshot are untouched.
--
-- ---------------------------------------------------------------------------
-- SCHEMA DRIFT THIS FILE REPAIRS (found while fetching the live bodies)
-- ---------------------------------------------------------------------------
-- `properties.all_sources` / `properties.active_sources` (text[]) exist on
-- PRODUCTION and are projected by the LIVE `browse_projection`, but they have NO
-- migration file on any branch that reached main -- migration 375's own header
-- flagged this as undocumented prod/repo drift, applied directly via the MCP by
-- a branch that never merged, and deliberately did not reproduce them. That
-- deferral is not survivable here: `create or replace view` CANNOT DROP a
-- column, so re-emitting 375's narrower body against production would fail
-- outright, while re-emitting the live body against a fresh CI replay would fail
-- on the missing base columns. This migration therefore adds both columns
-- additively and idempotently FIRST (no-op on prod, real on the replay) and then
-- carries them in `browse_projection` in their live positions. Nothing in
-- api/, toolkit/, frontend/src/, scripts/ or any database function reads or
-- writes them today -- verified -- so this closes the drift without adopting a
-- behaviour.
--
-- Everything else re-emitted here was fetched with pg_get_viewdef(oid, true) /
-- pg_get_functiondef(oid) from production and diffed against the migration that
-- last defines it. listings_public (420), properties_public (398),
-- listing_feed_public (370), pipeline_board_public (417),
-- browse_stats_properties (378), region_stats (103) and price_stat_growth (415)
-- all match their files exactly; only browse_projection had drifted. The bodies
-- below are those files' text with the per-m2 lines substituted mechanically --
-- NOT retyped. Migration 371 retyped a body from a file and silently regressed
-- migration 283's anon grant plus three covering indexes; 376 exists only to
-- repair that.
--
-- ---------------------------------------------------------------------------
-- DEPLOY ORDER
-- ---------------------------------------------------------------------------
-- This migration must be APPLIED BEFORE any code that references the new
-- objects merges. Objects that must exist first: functions
-- measure_price_per_m2(numeric, numeric, text, text) and
-- measure_price_per_m2_basis(text, text); the `price_per_m2_basis` column on
-- listings_public / properties_public / browse_projection / listing_feed_public
-- / browse_list / properties_map_mv / pipeline_board_public; the `ppm2_basis`
-- key in browse_stats_properties' and region_stats' JSON; and region_stats'
-- widened 7-argument signature.
--
-- ORDERING INSIDE THE FILE: browse_projection is re-emitted, then
-- rebuild_browse_list()/rebuild_properties_map_mv() run at the end so the new
-- column reaches the `browse_list` table and the `properties_map_mv` matview
-- immediately (both are `select * from browse_projection`) -- the ordering 363
-- and 375 already use. Their bodies are NOT retyped. Migration 254 is dead
-- history for properties_map_mv (superseded by 277) and is not an edit target.
--
-- browse_projection IS DROPPED AND RE-CREATED, NOT REPLACED -- but only where it
-- has to be. `create or replace view` may APPEND output columns; it may not
-- REPOSITION one. Live browse_projection carries all_sources (67) and
-- active_sources (68) ahead of home_city_id (69); on a fresh CI replay the last
-- definition is migration 375's, where home_city_id is column 67 and the drift
-- pair does not exist at all. The same `create or replace` therefore succeeds on
-- production (append #70) and fails on the replay with `cannot change name of
-- view column "home_city_id" to "all_sources"`, aborting migrations.yml under
-- ON_ERROR_STOP and skipping every later gate in that job. Adding the two BASE
-- columns (section 0) makes the SELECT legal but does not make the REPOSITION
-- legal. So section 3 opens with a guard that fires ONLY when the view is still
-- the narrow 375 shape: there it drops properties_map_mv (its one and only
-- dependent, via pg_rewrite -- verified live) and the view, and the statement
-- below becomes a plain create at the correct shape. On production the guard is
-- inert: nothing is dropped, the matview stays up, and the view keeps its ACL
-- and its comment. The comment is re-asserted after the create anyway, because
-- it documents the load-bearing `(select publication_gate_enabled())` wrapping
-- and a dropped view does not carry it forward.
--
-- THREE PHASES, AND THE LOCK ACCOUNTING THAT FORCES THEM.
--
--   Transaction 1  the `alter table properties add column` pair. ALTER TABLE
--                  takes ACCESS EXCLUSIVE on `properties` and holds it to
--                  commit, so it commits on its own -- milliseconds. Both ALTERs
--                  are additive and idempotent, so committing them
--                  independently is safe even if what follows rolls back.
--
--   Transaction 2  every function, view, comment and grant. `create or replace
--                  view` ALSO takes ACCESS EXCLUSIVE -- on listings_public,
--                  properties_public, browse_projection, listing_feed_public and
--                  pipeline_board_public -- and Postgres holds every lock until
--                  the transaction ends. This transaction is therefore SHORT: it
--                  contains no rebuild, so those five locks last milliseconds.
--
--   After it       the two read-model rebuilds, each as its own autocommit
--                  statement, exactly the way pg_cron jobs 6 and 7 run them
--                  today (`set statement_timeout='600s'; select
--                  public.rebuild_browse_list();`). Each builds a `_next`
--                  relation under ACCESS SHARE and takes ACCESS EXCLUSIVE only
--                  for its own drop+rename swap.
--
-- DO NOT FOLD THE REBUILDS BACK INTO TRANSACTION 2. Their last live run took
-- 446 s and 332 s (580 579 / 555 281 rows) -- ~13 minutes. Inside the DDL
-- transaction that is 13 minutes of ACCESS EXCLUSIVE on all five browser-read
-- views: `authenticated` carries statement_timeout=8s and `authenticator`
-- lock_timeout=8s, so every SPA read of Browse, every listing page, the kanban
-- and the extension lookup would not merely slow down, they would ERROR for the
-- whole window, and browse_list would be locked for the last ~5.5 minutes on top.
-- The rebuild functions' `_next`-then-swap design exists precisely to keep that
-- window at milliseconds; a long transaction throws the property away.
--
-- STATEMENT_TIMEOUT IS SET EXPLICITLY BEFORE THE REBUILDS, and that is not
-- decoration: the `postgres` role this migration is applied as (Supabase MCP)
-- reports statement_timeout = 2min, and 446 s is 3.7x that cap. Without the
-- raise, `select rebuild_browse_list()` is cancelled at 120 s -- deterministically,
-- not as a race -- and the error names the rebuild rather than the missing GUC.
--
-- BEFORE APPLYING: pause the browse read-model rebuild cron, or accept that a
-- concurrent cron tick holding `pg_try_advisory_lock(hashtext(...))` makes
-- rebuild_browse_list() SKIP with a notice rather than fail. The assertion after
-- the rebuilds turns that silent skip into a loud error instead of leaving
-- `browse_list` a column short.

begin;

set local lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 0. Drift repair: the two columns live browse_projection already projects.
--    TRANSACTION 1, on its own -- see the lock accounting in the header.
-- ---------------------------------------------------------------------------
alter table properties add column if not exists all_sources text[];
alter table properties add column if not exists active_sources text[];

comment on column properties.all_sources is
  'Every portal this property has ever been seen on. Applied directly to production '
  'with no migration file (see migration 375''s header); reconciled into the schema by '
  'migration 425 because browse_projection projects it and CREATE OR REPLACE VIEW cannot '
  'drop a column. No producer writes it yet.';

comment on column properties.active_sources is
  'The portals this property is currently active on. Same provenance as all_sources: '
  'prod-only drift reconciled by migration 425. No producer writes it yet.';

commit;

begin;

set local lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 1. THE MEASURE. Single expression, IMMUTABLE, PARALLEL SAFE, no SET clause --
--    source_trust_rank's declaration style, so the planner inlines it.
-- ---------------------------------------------------------------------------
create or replace function public.measure_price_per_m2(
  p_price numeric,
  p_area numeric,
  p_category_main text,
  p_category_type text
)
 RETURNS numeric
 LANGUAGE sql
 IMMUTABLE PARALLEL SAFE
AS $function$
    SELECT CASE
        WHEN p_price IS NULL OR p_area IS NULL OR p_area <= 0 THEN NULL
        WHEN p_category_type = 'pronajem' AND p_price >= 1000
            THEN round(p_price / p_area, 2)
        WHEN p_category_type = 'pronajem' THEN NULL
        WHEN p_category_main = 'pozemek'
             AND (p_category_type = 'prodej' OR p_category_type = 'drazba'
                  OR p_category_type = 'podil')
            THEN round(p_price / p_area, 2)
        WHEN (p_category_type = 'prodej' OR p_category_type = 'drazba'
              OR p_category_type = 'podil')
             AND p_price >= 100000
            THEN round(p_price / p_area, 2)
        ELSE NULL END
$function$;

comment on function public.measure_price_per_m2(numeric, numeric, text, text) is
  'THE per-m2 measure. Numerator: the listing/property price in CZK. Denominator: '
  'area_m2, which is POLYMORPHIC by design (floor area for byt/dum/komercni, PLOT '
  'area for pozemek) -- the basis is resolved from (category_main, category_type) by '
  'measure_price_per_m2_basis, never from price_unit. Unit: CZK per m2, per MONTH for '
  'the rent basis and capital otherwise. Returns round(x, 2): migration 200 documents '
  'why -- an unrounded numeric breaks the SPA keyset cursor''s equal-value tiebreaker '
  'and silently skips rows at the page seam. Returns NULL when area is NULL or <= 0, '
  'when price is NULL, when the basis is undecidable, or when the price is below its '
  'basis floor (rent < 1000, sale < 100000, land unfloored) -- the floors live here '
  'and NOT at the write boundary, so price_czk stays faithful to the source. '
  'No SET search_path: a SET clause blocks inlining and turns every predicate on the '
  'measure into a per-row function call.';

revoke all on function public.measure_price_per_m2(numeric, numeric, text, text) from public;
grant execute on function public.measure_price_per_m2(numeric, numeric, text, text)
  to anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 2. THE LABEL. Same resolution order as the measure -- they must never
--    disagree about which basis a row is on.
-- ---------------------------------------------------------------------------
create or replace function public.measure_price_per_m2_basis(
  p_category_main text,
  p_category_type text
)
 RETURNS text
 LANGUAGE sql
 IMMUTABLE PARALLEL SAFE
AS $function$
    SELECT CASE
        WHEN p_category_type = 'pronajem' THEN 'rent_monthly_czk_m2'
        WHEN p_category_main = 'pozemek'
             AND (p_category_type = 'prodej' OR p_category_type = 'drazba'
                  OR p_category_type = 'podil')
            THEN 'land_capital_czk_m2'
        WHEN p_category_type = 'prodej' OR p_category_type = 'drazba'
             OR p_category_type = 'podil'
            THEN 'sale_capital_czk_m2'
        ELSE NULL END
$function$;

comment on function public.measure_price_per_m2_basis(text, text) is
  'The four-token basis vocabulary for measure_price_per_m2: rent_monthly_czk_m2 '
  '(CZK per m2 per MONTH), land_capital_czk_m2 (CZK per m2 of PLOT), '
  'sale_capital_czk_m2 (CZK per m2 of floor area), or NULL when the basis is '
  'undecidable. Resolution order is rent-first so a rented plot is never labelled '
  'capital; the capital category_type list is an enumerated allowlist so an unknown '
  'future value yields a visible NULL rather than a silent guess. rent_monthly_czk_m2 '
  'is spelled identically to what rent_map_values is commented with, so the two '
  'systems share one word. NEVER derive the basis from listings.price_unit -- that '
  'column is a four-spelling duplicate of category_type, not a per-area unit.';

revoke all on function public.measure_price_per_m2_basis(text, text) from public;
grant execute on function public.measure_price_per_m2_basis(text, text)
  to anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 3. The four public views (still TRANSACTION 2 -- short, no rebuild inside it:
--    every `create or replace view` below takes ACCESS EXCLUSIVE and holds it to
--    COMMIT). Migration 420 / 398 / 375 / 370 bodies VERBATIM,
--    with only the per-m2 expression substituted and price_per_m2_basis
--    appended. broker_email / broker_phone stay `null::text` (migration 398):
--    restoring the real columns would re-expose broker PII on a browser-readable
--    view. tests/test_tenant_isolation_live.py re-derives that exemption from the
--    deparsed body on every run.
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
  st_y(geom::geometry) as lat,
  st_x(geom::geometry) as lng,
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
  measure_price_per_m2_basis(category_main, category_type) as price_per_m2_basis
from listings;
revoke all on listings_public from anon;
grant select on listings_public to authenticated;

create or replace view public.properties_public as
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
    measure_price_per_m2_basis(p.category_main, p.category_type) as price_per_m2_basis
from properties p
     left join listings l on l.id = p.repr_listing_ref_id
where p.status = 'active'::text
  and (not (select publication_gate_enabled()) or p.published_at is not null);
revoke all on public.properties_public from anon;
grant select on public.properties_public to authenticated;

-- browse_projection feeds BOTH read models (`select * from browse_projection`),
-- so price_per_m2_basis reaches browse_list and properties_map_mv through the
-- rebuilds at the end of this file.

-- SHAPE CONVERGENCE BEFORE THE REPLACE -- see the header. `create or replace
-- view` may append a column but may not REPOSITION one, and the live view
-- carries all_sources/active_sources ahead of home_city_id while a fresh replay
-- of migration 375 does not carry them at all. This guard fires ONLY on the
-- narrow 375 shape (i.e. on the replay), where it drops the view and its single
-- dependent so the statement below becomes a plain create at the right shape.
-- On production both columns are already there: nothing is dropped, the matview
-- stays up, and the view keeps its ACL. rebuild_properties_map_mv() at the end
-- of this file re-creates the matview with its indexes and grants.
do $$
begin
  if not exists (
    select 1 from pg_attribute
     where attrelid = 'public.browse_projection'::regclass
       and attname = 'all_sources' and not attisdropped
  ) then
    -- Plain plpgsql statements, deliberately not dynamic DDL: DDL built inside a
    -- string is invisible to tests/test_migration_rls_grants.py's statement
    -- scanner and would disarm every grant rule it holds over this migration.
    drop materialized view if exists properties_map_mv;
    drop view browse_projection;
  end if;
end $$;

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
    measure_price_per_m2(current_price_czk::numeric, area_m2, category_main, category_type) as price_per_m2,
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
    active_sources,
    home_city_id,
    measure_price_per_m2_basis(category_main, category_type) as price_per_m2_basis
from properties p
where status = 'active'::text
  and (not (select publication_gate_enabled()) or published_at is not null);
revoke all on browse_projection from anon;
grant select on browse_projection to authenticated;

-- Re-asserted because the guard above may have dropped the view, and a dropped
-- view does not carry its comment forward. Migration 276's text verbatim: it
-- documents the load-bearing gate wrapping that
-- tests/test_browse_read_path_guardrail.py pins.
comment on view browse_projection is
  'The ONE Browse read-model projection (migration 276): column contract + the '
  'publication-gate predicate in a single place. browse_list (5-min rebuild) and '
  'properties_map_mv (30-min rebuild, + lat/lng filter) are both materialized '
  'FROM this view by the rebuild functions in migration 277. The gate call MUST '
  'stay wrapped as (select publication_gate_enabled()) — a bare SECURITY DEFINER '
  'call cannot be inlined and runs per row (the PR-#707 incident); pinned by '
  'tests/test_browse_read_path_guardrail.py. Internal object: no anon grant.';

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
  l.obec_id,
  l.okres_id,
  l.region_id,
  st_y(l.geom::geometry) as lat,
  st_x(l.geom::geometry) as lng,
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
  -- Qualified `l.` (369 left these bare): the `properties` join added below
  -- brings its own area_m2/price_czk into scope, so the unqualified form is now
  -- ambiguous. Same expression, same output column — only the resolution is pinned.
  measure_price_per_m2(l.price_czk::numeric, l.area_m2::numeric, l.category_main, l.category_type) as price_per_m2,
  -- ---- appended by migration 370 ----
  -- `listing_id` is a deliberate alias of `id`, not redundancy: every Browse
  -- SELECT list, the React key, the maplibre feature-state id, the hover-sync
  -- set and the card-image batch all key on `listing_id` because that is what
  -- browse_list / properties_map_mv call the same surrogate (migration 343).
  -- Aliasing it here means the frontend's column contract is IDENTICAL across
  -- both read models and the single-portal swap touches only the relation name.
  l.id as listing_id,
  -- Carried so operator curation (tags, collections, pipeline, merge) keeps
  -- working from a mirrored card, and as the detail-link fallback. NULLABLE by
  -- rule #19 (a freshly drained listing is attached out-of-band), which is why
  -- it must NOT be the keyset tiebreak in this mode — `listing_id` is.
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
  -- Listing-grain equivalents of two browse_projection expressions. Both are
  -- pure functions of columns already on the row, so they are exact at this
  -- grain rather than the property-grain approximation browse_list carries.
  concat_ws(', '::text, l.street, l.locality) as place_search_text,
  case when l.is_active
       then greatest(0, floor(extract(epoch from now() - l.first_seen_at) / 86400::numeric)::integer)
       else greatest(0, floor(extract(epoch from l.last_seen_at - l.first_seen_at) / 86400::numeric)::integer)
  end as tom_days,
  l.mf_gross_yield_pct,
  -- Property-grain precomputes. These exist ONLY so the corresponding Browse
  -- filters keep working unchanged in single-portal mode; none of them is
  -- displayed on a card, so root cause 3's golden-record leakage (a DISPLAYED
  -- field lifted from a different portal's sibling listing) cannot occur
  -- through them. They are genuinely property-grain facts by construction:
  -- city proximity/population is a function of location, and the price-change
  -- and last-change series are maintained per property.
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
  measure_price_per_m2_basis(l.category_main, l.category_type) as price_per_m2_basis
from listings l
join properties p on p.id = l.property_id
where p.status = 'active'
  and (not (select publication_gate_enabled()) or p.published_at is not null);
revoke all on listing_feed_public from anon;
grant select on listing_feed_public to authenticated;

-- ---------------------------------------------------------------------------
-- 4. browse_stats_properties -- migration 378's body, four edits:
--      * `filtered` also selects l.price_per_m2, l.category_main, l.category_type
--      * the Kc/m2 bounds read the stored rounded column instead of recomputing
--        the division over the same row (this is the T1 collapse: 378 ignored a
--        column it had already materialised)
--      * all ten inline divisions in ppm2_pct + disposition_dist become
--        `price_per_m2`
--      * a new `ppm2_basis` key: the cohort's single basis, or the literal
--        'mixed'. Mixed is reachable in ONE click -- category_type_filter is
--        nullable by architectural rule 22 ("Vse"), so a cohort can legitimately
--        pool a capital sale and a monthly rent, and the consumer must be told.
--    Signature unchanged, so CREATE OR REPLACE preserves the ACL; the grants are
--    re-asserted anyway (postgres + authenticated + service_role, PUBLIC/anon
--    deliberately absent since the public-release hardening).
--
--    BEHAVIOUR CHANGE, stated plainly: the Kc/m2 bounds now exclude rows whose
--    price is below their basis floor, because the measure returns NULL there.
--    A saved Browse view with a Kc/m2 maximum stops matching a 136 Kc commercial
--    rental. That is the intent.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.browse_stats_properties(districts_filter text[] DEFAULT NULL::text[], dispositions_filter text[] DEFAULT NULL::text[], price_min_filter integer DEFAULT NULL::integer, price_max_filter integer DEFAULT NULL::integer, area_min_filter integer DEFAULT NULL::integer, area_max_filter integer DEFAULT NULL::integer, active_only_filter boolean DEFAULT false, last_seen_min_days integer DEFAULT NULL::integer, last_seen_max_days integer DEFAULT NULL::integer, first_seen_min_days integer DEFAULT NULL::integer, first_seen_max_days integer DEFAULT NULL::integer, tom_days_min integer DEFAULT NULL::integer, tom_days_max integer DEFAULT NULL::integer, has_balcony_filter boolean DEFAULT NULL::boolean, has_lift_filter boolean DEFAULT NULL::boolean, has_parking_filter boolean DEFAULT NULL::boolean, inactive_only_filter boolean DEFAULT false, furnished_filter text[] DEFAULT NULL::text[], terrace_filter boolean DEFAULT NULL::boolean, cellar_filter boolean DEFAULT NULL::boolean, garage_filter boolean DEFAULT NULL::boolean, category_sub_cb_filter integer DEFAULT NULL::integer, building_type_filter text[] DEFAULT NULL::text[], tag_ids bigint[] DEFAULT NULL::bigint[], category_main_filter text[] DEFAULT NULL::text[], category_type_filter text DEFAULT NULL::text, bbox_west double precision DEFAULT NULL::double precision, bbox_south double precision DEFAULT NULL::double precision, bbox_east double precision DEFAULT NULL::double precision, bbox_north double precision DEFAULT NULL::double precision, ownership_filter text[] DEFAULT NULL::text[], estate_area_min_filter double precision DEFAULT NULL::double precision, estate_area_max_filter double precision DEFAULT NULL::double precision, usable_area_min_filter double precision DEFAULT NULL::double precision, usable_area_max_filter double precision DEFAULT NULL::double precision, parking_lots_min_filter integer DEFAULT NULL::integer, garden_area_min_filter double precision DEFAULT NULL::double precision, garden_area_max_filter double precision DEFAULT NULL::double precision, condition_match_filter text[] DEFAULT NULL::text[], districts_context_filter text[] DEFAULT NULL::text[], city_index_rules jsonb DEFAULT NULL::jsonb, city_pop_min integer DEFAULT NULL::integer, city_pop_max integer DEFAULT NULL::integer, city_proximity jsonb DEFAULT NULL::jsonb, price_per_m2_min double precision DEFAULT NULL::double precision, price_per_m2_max double precision DEFAULT NULL::double precision, portal_filter text[] DEFAULT NULL::text[], mf_gross_yield_pct_min double precision DEFAULT NULL::double precision, mf_gross_yield_pct_max double precision DEFAULT NULL::double precision, near_pop_5km_min integer DEFAULT NULL::integer, near_pop_15km_min integer DEFAULT NULL::integer, near_jobs_5km_min double precision DEFAULT NULL::double precision, near_jobs_15km_min double precision DEFAULT NULL::double precision, near_youth_5km_min double precision DEFAULT NULL::double precision, near_youth_15km_min double precision DEFAULT NULL::double precision, near_overall_5km_min double precision DEFAULT NULL::double precision, near_overall_15km_min double precision DEFAULT NULL::double precision, districts_excluded_filter boolean[] DEFAULT NULL::boolean[], subtype_filter text[] DEFAULT NULL::text[], recently_added_days integer DEFAULT NULL::integer, recently_changed_days integer DEFAULT NULL::integer, obec_ids_filter bigint[] DEFAULT NULL::bigint[], districts_levels text[] DEFAULT NULL::text[], districts_ids bigint[] DEFAULT NULL::bigint[], building_condition_level_min integer DEFAULT NULL::integer, building_condition_level_max integer DEFAULT NULL::integer, apartment_condition_level_min integer DEFAULT NULL::integer, apartment_condition_level_max integer DEFAULT NULL::integer, price_change_count_min integer DEFAULT NULL::integer, price_change_window_days integer DEFAULT NULL::integer, total_price_change_pct_filter double precision DEFAULT NULL::double precision, with_estimates boolean DEFAULT false, include_no_price boolean DEFAULT false, property_ids_filter bigint[] DEFAULT NULL::bigint[])
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
begin
  return (
  with filtered as (
    select l.sreality_id, l.first_seen_at, l.last_seen_at, l.is_active, l.price_czk, l.area_m2, l.disposition, l.tom_days, l.price_per_m2, l.category_main, l.category_type
    from browse_list l
    where
          (not active_only_filter   or l.is_active = true)
      and (not inactive_only_filter or l.is_active = false)
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
                 coalesce(districts_context_filter, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)])),
                 coalesce(districts_levels, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_ids, array_fill(null::bigint, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, ctx, excl, lvl, admin_id, ord)
          where not coalesce(excl, false)
            and case
              when lvl = 'obec'  and admin_id is not null then l.obec_id   = admin_id
              when lvl = 'okres' and admin_id is not null then l.okres_id  = admin_id
              when lvl = 'kraj'  and admin_id is not null then l.region_id = admin_id
              when lvl = 'locality' then (admin_id is null or l.obec_id = admin_id) and l.place_search_text ilike '%' || needle || '%'
              else (l.district ilike '%' || needle || '%' or l.place_search_text ilike '%' || needle || '%'
                    or l.okres ilike '%' || needle || '%' or l.region ilike '%' || needle || '%')
                and (ctx is null or ctx = '' or l.district ilike '%' || ctx || '%' or l.place_search_text ilike '%' || ctx || '%'
                     or l.okres ilike '%' || ctx || '%' or l.region ilike '%' || ctx || '%')
            end
        )
      )
      and (
        districts_filter is null or array_length(districts_filter, 1) is null
        or not exists (
          select 1 from unnest(districts_filter,
                 coalesce(districts_context_filter, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)])),
                 coalesce(districts_levels, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_ids, array_fill(null::bigint, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, ctx, excl, lvl, admin_id, ord)
          where coalesce(excl, false)
            and case
              when lvl = 'obec'  and admin_id is not null then l.obec_id   = admin_id
              when lvl = 'okres' and admin_id is not null then l.okres_id  = admin_id
              when lvl = 'kraj'  and admin_id is not null then l.region_id = admin_id
              when lvl = 'locality' then (admin_id is null or l.obec_id = admin_id) and l.place_search_text ilike '%' || needle || '%'
              else (l.district ilike '%' || needle || '%' or l.place_search_text ilike '%' || needle || '%'
                    or l.okres ilike '%' || needle || '%' or l.region ilike '%' || needle || '%')
                and (ctx is null or ctx = '' or l.district ilike '%' || ctx || '%' or l.place_search_text ilike '%' || ctx || '%'
                     or l.okres ilike '%' || ctx || '%' or l.region ilike '%' || ctx || '%')
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
      and (portal_filter is null or array_length(portal_filter, 1) is null or l.source = any(portal_filter))
      and (
        ownership_filter is null or array_length(ownership_filter, 1) is null
        or l.ownership = any(ownership_filter)
        or ('__unknown__' = any(ownership_filter)
            and (l.ownership is null or not (l.ownership = any(array['osobni','druzstevni','statni']))))
      )
      and (estate_area_min_filter  is null or l.estate_area   >= estate_area_min_filter)
      and (estate_area_max_filter  is null or l.estate_area   <= estate_area_max_filter)
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
      and ((city_index_rules is null or jsonb_array_length(city_index_rules) = 0)
        or (l.home_city_id is not null
            and not exists (select 1 from jsonb_array_elements(coalesce(city_index_rules, '[]'::jsonb)) r
                where not exists (select 1 from city_index_values_public v where v.city_id = l.home_city_id and v.index_name = r->>'index_name'
                  and case coalesce(r->>'op', '>=')
                        when '>=' then v.value >= (r->>'value')::numeric
                        when '<=' then v.value <= (r->>'value')::numeric
                        when '>'  then v.value >  (r->>'value')::numeric
                        when '<'  then v.value <  (r->>'value')::numeric
                        when '==' then v.value =  (r->>'value')::numeric
                        when '!=' then v.value <> (r->>'value')::numeric
                        else           v.value >= (r->>'value')::numeric
                      end))))
      and (city_proximity is null or (l.lat is not null and l.lng is not null and exists (
            select 1 from curated_cities_public c
            where st_dwithin(st_setsrid(st_makepoint(l.lng, l.lat), 4326)::geography, st_setsrid(st_makepoint(c.lng, c.lat), 4326)::geography, ((city_proximity ->> 'radius_km')::int * 1000))
              and ((city_proximity ->> 'population_min')::int is null or c.population >= (city_proximity ->> 'population_min')::int)
              and not exists (select 1 from jsonb_array_elements(coalesce(city_proximity -> 'index_rules', '[]'::jsonb)) r
                where not exists (select 1 from city_index_values_public v where v.city_id = c.city_id and v.index_name = r->>'index_name'
                  and case coalesce(r->>'op', '>=')
                        when '>=' then v.value >= (r->>'value')::numeric
                        when '<=' then v.value <= (r->>'value')::numeric
                        when '>'  then v.value >  (r->>'value')::numeric
                        when '<'  then v.value <  (r->>'value')::numeric
                        when '==' then v.value =  (r->>'value')::numeric
                        when '!=' then v.value <> (r->>'value')::numeric
                        else           v.value >= (r->>'value')::numeric
                      end)))))
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

revoke execute on function public.browse_stats_properties(
  text[], text[], integer, integer, integer, integer, boolean, integer,
  integer, integer, integer, integer, integer, boolean, boolean, boolean,
  boolean, text[], boolean, boolean, boolean, integer, text[], bigint[],
  text[], text, double precision, double precision, double precision, double
  precision, text[], double precision, double precision, double precision,
  double precision, integer, double precision, double precision, text[],
  text[], jsonb, integer, integer, jsonb, double precision, double
  precision, text[], double precision, double precision, integer, integer,
  double precision, double precision, double precision, double precision,
  double precision, double precision, boolean[], text[], integer, integer,
  bigint[], text[], bigint[], integer, integer, integer, integer, integer,
  integer, double precision, boolean, boolean, bigint[]
) from public;

grant execute on function public.browse_stats_properties(
  text[], text[], integer, integer, integer, integer, boolean, integer,
  integer, integer, integer, integer, integer, boolean, boolean, boolean,
  boolean, text[], boolean, boolean, boolean, integer, text[], bigint[],
  text[], text, double precision, double precision, double precision, double
  precision, text[], double precision, double precision, double precision,
  double precision, integer, double precision, double precision, text[],
  text[], jsonb, integer, integer, jsonb, double precision, double
  precision, text[], double precision, double precision, integer, integer,
  double precision, double precision, double precision, double precision,
  double precision, double precision, boolean[], text[], integer, integer,
  bigint[], text[], bigint[], integer, integer, integer, integer, integer,
  integer, double precision, boolean, boolean, bigint[]
) to authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 5. region_stats -- the worst live basis failure in the schema. Today it has NO
--    category parameter at all, so sale flats, monthly rentals, houses and land
--    pool into ONE Kc/m2 distribution unconditionally. DROP + CREATE (in this
--    same transaction) because two defaulted parameters are appended: adding
--    them with CREATE OR REPLACE alone would leave the old 5-argument function
--    in place and make every 5-argument call ambiguous.
--
--    Verified before dropping: region_stats has ZERO callers in api/, toolkit/,
--    frontend/src/, scripts/ and chrome-extension/ -- the only repo mention is a
--    doc comment in DispositionBoxPlots.tsx. `region_active_by_day`, defined in
--    the same file (103), is untouched.
--
--    103 granted this to `anon`; the live ACL has no anon and no PUBLIC (the
--    public-release hardening revoked it). DROP wipes the ACL and CREATE hands
--    EXECUTE back to PUBLIC by default, so the LIVE posture is restored below --
--    not 103's.
-- ---------------------------------------------------------------------------
drop function if exists public.region_stats(text[], double precision, double precision, integer, integer);

create function region_stats(
  districts_filter        text[]           default null,
  center_lng              double precision default null,
  center_lat              double precision default null,
  radius_m                integer          default null,
  category_sub_cb_filter  integer          default null,
  category_main_filter    text[]           default null,
  category_type_filter    text             default null
)
returns jsonb
language sql
stable
security invoker
as $$
  with filtered as (
    select
      first_seen_at, last_seen_at, is_active,
      price_czk, area_m2, disposition,
      price_per_m2, price_per_m2_basis
    from properties_public
    where
      case
        when districts_filter is not null and array_length(districts_filter, 1) > 0 then
          district = any(districts_filter)
        when center_lng is not null and center_lat is not null and radius_m is not null then
          lat is not null and lng is not null
          and ST_DWithin(
                ST_SetSRID(ST_MakePoint(lng, lat), 4326)::geography,
                ST_SetSRID(ST_MakePoint(center_lng, center_lat), 4326)::geography,
                radius_m
              )
        else false
      end
      and (category_sub_cb_filter is null or category_sub_cb = category_sub_cb_filter)
      and (category_main_filter is null or array_length(category_main_filter, 1) is null
           or category_main = any(category_main_filter))
      and (category_type_filter is null or category_type = category_type_filter)
  ),
  active_only as (
    select * from filtered where is_active = true
  ),
  price_pct as (
    select
      percentile_cont(0.25) within group (order by price_czk)::int as p25,
      percentile_cont(0.50) within group (order by price_czk)::int as p50,
      percentile_cont(0.75) within group (order by price_czk)::int as p75
    from active_only
    where price_czk is not null
  ),
  ppm2_pct as (
    select
      percentile_cont(0.25) within group (order by price_per_m2)::int as p25,
      percentile_cont(0.50) within group (order by price_per_m2)::int as p50,
      percentile_cont(0.75) within group (order by price_per_m2)::int as p75
    from active_only
    where price_per_m2 is not null
  ),
  ppm2_basis as (
    select case
             when count(distinct price_per_m2_basis) = 0 then null
             when count(distinct price_per_m2_basis) = 1 then min(price_per_m2_basis)
             else 'mixed'
           end as basis
    from active_only
    where price_per_m2 is not null
  ),
  disposition_dist as (
    select
      coalesce(disposition, 'unspecified') as disposition,
      count(*)::int as n,
      percentile_cont(0.50) within group (order by price_czk)::int as median_price,
      percentile_cont(0.50) within group (
        order by price_per_m2
      )::int as median_ppm2,
      percentile_cont(0.50) within group (order by area_m2)::int as median_area,
      count(price_per_m2)::int as ppm2_n,
      min(price_per_m2)::int as ppm2_min,
      percentile_cont(0.25) within group (
        order by price_per_m2
      )::int as ppm2_p25,
      percentile_cont(0.75) within group (
        order by price_per_m2
      )::int as ppm2_p75,
      max(price_per_m2)::int as ppm2_max
    from active_only
    group by disposition
    order by n desc, disposition asc
  ),
  delisted as (
    select
      extract(epoch from (last_seen_at - first_seen_at)) / 86400.0 as days_alive
    from filtered
    where is_active = false
  ),
  tom as (
    select
      count(*)::int as n,
      percentile_cont(0.50) within group (order by days_alive)::numeric(10,1) as median_days
    from delisted
  )
  select jsonb_build_object(
    'total_active',          (select count(*)::int from active_only),
    'total_ever',             (select count(*)::int from filtered),
    'last_new_first_seen',   (select max(first_seen_at) from filtered),
    'price',                 (select case when p50 is not null
                                          then jsonb_build_object('p25', p25, 'p50', p50, 'p75', p75)
                                          else null end
                              from price_pct),
    'ppm2',                  (select case when p50 is not null
                                          then jsonb_build_object('p25', p25, 'p50', p50, 'p75', p75)
                                          else null end
                              from ppm2_pct),
    'ppm2_basis',            (select basis from ppm2_basis),
    'dispositions',          coalesce(
                               (select jsonb_agg(jsonb_build_object(
                                  'disposition',   disposition,
                                  'n',             n,
                                  'median_price',  median_price,
                                  'median_ppm2',   median_ppm2,
                                  'median_area',   median_area,
                                  'ppm2_box',      case when ppm2_n > 0 then
                                                     jsonb_build_object(
                                                       'n',      ppm2_n,
                                                       'min',    ppm2_min,
                                                       'p25',    ppm2_p25,
                                                       'median', median_ppm2,
                                                       'p75',    ppm2_p75,
                                                       'max',    ppm2_max
                                                     )
                                                   else null end
                                )) from disposition_dist),
                               '[]'::jsonb
                             ),
    'tom_median_days',       (select median_days from tom),
    'tom_n',                 (select n from tom)
  );
$$;
revoke all on function public.region_stats(
  text[], double precision, double precision, integer, integer, text[], text
) from public, anon;
grant execute on function public.region_stats(
  text[], double precision, double precision, integer, integer, text[], text
) to authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 6. pipeline_board_public -- migration 417's body plus the three columns the
--    kanban needs to render a per-m2 figure with its label (417 omitted
--    price_per_m2 AND category_type, so the board is the one surface with no
--    basis available at all). `with (security_invoker = true)` is load-bearing:
--    the view inherits its tenant scoping from property_pipeline_public, and
--    tests/test_tenant_isolation_live.py::_TENANT_VIEWS already asserts it (417
--    registered it; nothing to add there).
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
  p.price_per_m2_basis
from property_pipeline_public pp
left join properties_public p on p.property_id = pp.property_id;
revoke all on pipeline_board_public from public, anon;
grant select on pipeline_board_public to authenticated;

-- ---------------------------------------------------------------------------
-- 7. The stored per-m2 columns get their units and period in the catalog, in the
--    canonical wording of scraper/price_stats_metrics.gross_yield_pct's
--    docstring: "per-m2 cancels, so units don't matter; rent is Kc/m2/month,
--    sale is Kc/m2".
-- ---------------------------------------------------------------------------
comment on column price_stat_observations.price is
  'A RATE, not an absolute price, despite the column name: CZK per m2. For '
  'category_type_cb = 1 (sale) it is capital CZK/m2; for category_type_cb = 2 (rent) '
  'it is CZK/m2 per MONTH. Basis vocabulary: measure_price_per_m2_basis.';

comment on column price_stat_city_metrics.sale_latest_price is
  'Capital CZK per m2 (sale basis, sale_capital_czk_m2) at the latest observed month '
  'in the window -- a rate, not a property price.';

comment on column price_stat_city_metrics.rent_latest_price is
  'CZK per m2 per MONTH (rent basis, rent_monthly_czk_m2) at the latest observed month '
  'in the window -- a rate, not a monthly rent.';

comment on column price_stat_city_metrics.gross_yield_pct is
  'Annual gross yield in percent: 12 * rent_latest_price / sale_latest_price * 100. '
  'Per-m2 cancels, so units do not matter; rent is CZK/m2/month, sale is CZK/m2. '
  'Rounded to 2dp by scraper/price_stats_metrics.gross_yield_pct -- price_stat_growth '
  'rounds identically (migration 425) so the two can no longer disagree.';

comment on column rent_map_values.ref_rent_per_m2 is
  'MF Cenova mapa najemneho reference rent: CZK per m2 per MONTH '
  '(rent_monthly_czk_m2). The period is monthly even though the column name states '
  'only the area unit.';

comment on column rent_map_values.ref_rent_novostavba_per_m2 is
  'MF Cenova mapa najemneho reference rent for new builds: CZK per m2 per MONTH '
  '(rent_monthly_czk_m2).';

comment on column rent_map_adjustments.czk_per_m2 is
  'Additive adjustment to the MF reference rent: CZK per m2 per MONTH '
  '(rent_monthly_czk_m2), same basis and period as rent_map_values.ref_rent_per_m2.';

-- price_stat_growth's gross_yield_pct was UNROUNDED while the stored
-- price_stat_city_metrics.gross_yield_pct is rounded to 2dp by
-- scraper/price_stats_metrics.py -- same municipality, two answers. Migration
-- 415's body verbatim with that one expression rounded; yield_change_pp_pa is a
-- delta of two yields and stays unrounded so the rounding cannot accumulate into
-- the derivative.
create or replace function price_stat_growth(
  p_dataset_id bigint,
  p_from text default null,
  p_to text default null
)
returns table(
  obec_id bigint, locality_name text,
  sale_latest_price integer, sale_cagr_pct double precision, sale_min_active integer,
  rent_latest_price integer, rent_cagr_pct double precision, rent_min_active integer,
  gross_yield_pct double precision, yield_change_pp_pa double precision
)
language sql stable as $function$
  with bounds as (
    select
      case when p_from is null then null
           else split_part(p_from, '-', 1)::int * 12
                + split_part(p_from, '-', 2)::int - 1 end as from_idx,
      case when p_to is null then null
           else split_part(p_to, '-', 1)::int * 12
                + split_part(p_to, '-', 2)::int - 1 end as to_idx
  ),
  obs as (
    select o.obec_id, o.locality_name, o.category_type_cb,
           (o.year * 12 + o.month - 1) as ymi, o.price, o.active_count
      from price_stat_observations_public o, bounds b
     where o.dataset_id = p_dataset_id
       and o.price is not null and o.price > 0
       and o.obec_id is not null
       and (b.from_idx is null or (o.year * 12 + o.month - 1) >= b.from_idx)
       and (b.to_idx is null or (o.year * 12 + o.month - 1) <= b.to_idx)
  ),
  agg as (
    select obec_id, max(locality_name) as locality_name, category_type_cb,
           min(ymi) as start_ymi, max(ymi) as end_ymi,
           least(
             (array_agg(active_count order by ymi))[1],
             (array_agg(active_count order by ymi desc))[1]
           ) as min_active,
           (array_agg(price order by ymi))[1] as start_price,
           (array_agg(price order by ymi desc))[1] as end_price
      from obs group by obec_id, category_type_cb
  ),
  piv as (
    select obec_id,
           max(locality_name) as locality_name,
           max(end_price)   filter (where category_type_cb = 1) as sale_end,
           max(start_price) filter (where category_type_cb = 1) as sale_start,
           max(end_ymi)     filter (where category_type_cb = 1) as sale_end_ymi,
           min(start_ymi)   filter (where category_type_cb = 1) as sale_start_ymi,
           max(min_active)  filter (where category_type_cb = 1) as sale_min_active,
           max(end_price)   filter (where category_type_cb = 2) as rent_end,
           max(start_price) filter (where category_type_cb = 2) as rent_start,
           max(end_ymi)     filter (where category_type_cb = 2) as rent_end_ymi,
           min(start_ymi)   filter (where category_type_cb = 2) as rent_start_ymi,
           max(min_active)  filter (where category_type_cb = 2) as rent_min_active
      from agg group by obec_id
  )
  select
    p.obec_id,
    p.locality_name,
    p.sale_end::int,
    case when p.sale_end_ymi - p.sale_start_ymi >= 12 and p.sale_start > 0
         then (power(p.sale_end::numeric / p.sale_start,
                     12.0 / (p.sale_end_ymi - p.sale_start_ymi)) - 1) * 100 end,
    p.sale_min_active::int,
    p.rent_end::int,
    case when p.rent_end_ymi - p.rent_start_ymi >= 12 and p.rent_start > 0
         then (power(p.rent_end::numeric / p.rent_start,
                     12.0 / (p.rent_end_ymi - p.rent_start_ymi)) - 1) * 100 end,
    p.rent_min_active::int,
    case when p.sale_end > 0 and p.rent_end is not null
         then round((12.0 * p.rent_end / p.sale_end * 100)::numeric, 2)::double precision end,
    case when p.sale_end > 0 and p.sale_start > 0
              and p.rent_end is not null and p.rent_start is not null
              and greatest(p.sale_end_ymi, p.rent_end_ymi)
                  - least(p.sale_start_ymi, p.rent_start_ymi) >= 12
         then ((12.0 * p.rent_end / p.sale_end * 100)
               - (12.0 * p.rent_start / p.sale_start * 100))
              / ((greatest(p.sale_end_ymi, p.rent_end_ymi)
                  - least(p.sale_start_ymi, p.rent_start_ymi)) / 12.0) end
  from piv p
  where exists (select 1 from admin_boundaries_public b where b.id = p.obec_id);
$function$;
revoke all on function price_stat_growth(bigint, text, text) from public, anon;
grant execute on function price_stat_growth(bigint, text, text) to authenticated;

-- browse_stats(...) (migration 083) is superseded by browse_stats_properties and
-- has ZERO callers in api/, toolkit/, frontend/src/ and scripts/. The charter
-- proposed dropping it; dropping is destructive and this sprint carries no
-- operator sign-off for a destructive step, so the intent is recorded
-- REVERSIBLY here instead and the DROP is a follow-up needing operator approval.
-- Matched by name so this is inert on a schema where 083's signature differs.
do $$
declare v_oid oid;
begin
  for v_oid in
    select p.oid from pg_proc p join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public' and p.proname = 'browse_stats'
  loop
    execute format('comment on function %s is %L', v_oid::regprocedure,
      'SUPERSEDED and UNREFERENCED. Replaced by browse_stats_properties (property '
      'grain, migration 094 onward); zero callers in api/, toolkit/, frontend/src/ '
      'and scripts/ as of migration 425. It still carries a hand-typed listing-grain '
      'price/area division that the per-m2 measure-unification program (W4) '
      'deliberately did NOT migrate. Do not resurrect it and do not add a caller: '
      'the intended end state is DROP, held back only for want of operator sign-off '
      'on a destructive step.');
  end loop;
end $$;

commit;

-- ---------------------------------------------------------------------------
-- 8. Materialise the new column into both read models. Both are
--    `select * from browse_projection`; their bodies are NOT retyped here.
--
--    OUTSIDE THE DDL TRANSACTION, ON PURPOSE -- the `commit;` above is
--    load-bearing. Each rebuild builds a `_next` relation under ACCESS SHARE
--    and takes ACCESS EXCLUSIVE only for its own drop+rename swap. Run inside
--    transaction 2 instead, the five `create or replace view` locks taken there
--    would be held for the rebuilds' full ~13 minutes (446 s + 332 s on the last
--    live run) and every browser read of Browse / listing detail / the kanban
--    would ERROR on the 8 s statement_timeout that `authenticated` carries, not
--    merely queue. Autocommit statements are exactly how pg_cron jobs 6 and 7
--    run these functions against live traffic today.
--
--    statement_timeout is raised the way those two jobs raise it. The `postgres`
--    role this file is applied as reports statement_timeout = 2min; 446 s is
--    3.7x that, so without the raise rebuild_browse_list() is cancelled at 120 s
--    every single time. 900 s leaves headroom over the 600 s the cron uses.
-- ---------------------------------------------------------------------------
set statement_timeout = '900s';
set lock_timeout = '30s';

select rebuild_browse_list();
select rebuild_properties_map_mv();

reset statement_timeout;
reset lock_timeout;

-- ---------------------------------------------------------------------------
-- 9. Post-rebuild assertions, in their own short transaction.
-- ---------------------------------------------------------------------------
begin;

set local lock_timeout = '5s';

-- Both rebuilds SKIP (notice, not error) when a cron tick already holds their
-- advisory lock. A skip here would leave browse_list and properties_map_mv one
-- column short of browse_projection, and the only symptom would be a 400 from
-- PostgREST the first time a consumer asks for price_per_m2_basis. Fail loudly
-- instead.
do $$
declare v_missing text;
begin
  select string_agg(t, ', ') into v_missing from (
    select 'browse_list' as t where not exists (
      select 1 from pg_attribute a
       where a.attrelid = 'public.browse_list'::regclass
         and a.attname = 'price_per_m2_basis' and not a.attisdropped)
    union all
    select 'properties_map_mv' where not exists (
      select 1 from pg_attribute a
       where a.attrelid = 'public.properties_map_mv'::regclass
         and a.attname = 'price_per_m2_basis' and not a.attisdropped)
  ) s;
  if v_missing is not null then
    raise exception
      'read model(s) % lack price_per_m2_basis -- a concurrent rebuild held the '
      'advisory lock and this one skipped; pause the browse rebuild cron and '
      'retry migration 425', v_missing;
  end if;
end $$;

-- rebuild_properties_map_mv DROP+CREATEs properties_map_mv, which inherits the
-- default ACL; assert it did not re-grant MAINTAIN to a browser role (mirrors
-- migration 342/343/363's guard). MAINTAIN is PG17+, so skip on an older replay.
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
