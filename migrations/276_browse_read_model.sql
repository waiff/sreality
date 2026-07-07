-- 276_browse_read_model.sql
--
-- Unified Browse read model, slice 1 of docs/design/browse-read-model.md
-- (merged PR #708; operator-approved 2026-07-07 with default sort switching to
-- first_seen_at DESC and a 5-minute list-refresh cadence).
--
-- WHAT: one shared projection (`browse_projection`) = the Browse column
-- contract + the publication-gate predicate in ONE place, feeding TWO read
-- models: `browse_list` (NEW — every active property incl. the ~11.7k
-- coordinate-less ones; serves cards / table / counts / stats) and the existing
-- `properties_map_mv` (re-pointed onto the projection by migration 277's
-- rebuild function; still geo-clustered, still lat/lng-only).
--
-- WHY: cards/table/count/stats read the live 450k-row `properties` OLTP table
-- through `properties_public` today. Every (category x sort x filter) shape
-- needs its own index on the churned hot table (31 indexes / 985 MB vs a
-- 336 MB heap after PR #707), `last_seen_at` mutates under the reader's
-- infinite scroll, and exact counts are unaffordable (the "~N" estimate). A
-- compact rebuilt-wholesale snapshot serves the same shapes with a LEAN index
-- set, zero per-row write amplification, stable keyset pagination, and 57 ms
-- exact counts (measured on properties_map_mv's covering index, fully cold).
--
-- DEVIATION from the design doc (improvement, not drift): `browse_list` is an
-- UNLOGGED TABLE, not a materialized view. A 5-minute blue-green MATVIEW
-- rebuild would write the full heap + indexes through WAL 288x/day
-- (~75-100 GB/day) into an instance the PR-#707 investigation showed is
-- already I/O-saturated. UNLOGGED skips WAL for exactly this disposable,
-- rebuilt-every-5-min derived data. Trade-off: after a crash recovery Postgres
-- truncates unlogged tables, so Browse lists would be empty for <=5 minutes
-- until the next rebuild tick — acceptable for a read model that is never the
-- source of truth. (The 30-min map stays a regular matview: its WAL cost is
-- the long-accepted status quo and it predates this migration.) PostgREST
-- serves a table exactly like a matview; anon gets SELECT only.
--
-- The initial CREATE here is `WITH NO DATA`-equivalent (LIMIT 0): the real
-- build happens on the first rebuild_browse_list() run (migration 277) within
-- 5 minutes, so this migration applies in milliseconds and CI's empty-schema
-- replay stays trivial. NOTHING reads browse_list until the frontend re-point
-- PR lands (after live verification).

-------------------------------------------------------------------
-- 1. The shared projection: Browse column contract + gate, ONCE.
-------------------------------------------------------------------
-- Column list = the properties_map_mv projection (migration 254 /
-- refresh_map_mv.py) verbatim; the ONLY difference from the map build is that
-- no lat/lng filter is applied here (the map's rebuild adds it back).
-- `street` is bare p.street (the group-best denorm, migration 183) — NOT the
-- view's COALESCE(p.street, l.street): the listings join is not free at
-- 450k-row materialization scale, and the ~4.2k rows (0.95%, sampled live
-- 2026-07-07) where the fallback fires are a stale-denorm data bug fixed at
-- the source (see step 5 below), not a reason to carry a join forever.
-- NOT granted to anon: this is an internal contract object read only by the
-- rebuild functions (SECURITY DEFINER) and migrations.

create or replace view browse_projection as
select
  p.id                         as property_id,
  p.repr_listing_id            as sreality_id,
  p.first_seen_at, p.last_seen_at, p.is_active,
  p.category_main, p.category_type,
  p.current_price_czk          as price_czk,
  p.area_m2, p.disposition, p.locality, p.district,
  p.locality_district_id, p.locality_region_id,
  p.lat, p.lng,
  p.has_balcony, p.has_parking, p.has_lift, p.building_type, p.condition,
  p.energy_rating, p.estate_area, p.usable_area, p.garden_area, p.category_sub_cb,
  p.furnished, p.terrace, p.cellar, p.garage, p.parking_lots, p.ownership,
  case when p.is_active
       then greatest(0, floor(extract(epoch from now() - p.first_seen_at) / 86400::numeric)::integer)
       else greatest(0, floor(extract(epoch from p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
  end as tom_days,
  case when p.area_m2 is not null and p.area_m2 > 0::numeric and p.current_price_czk is not null
       then round(p.current_price_czk::numeric / p.area_m2, 2)
       else null::numeric end as price_per_m2,
  p.building_condition_level, p.apartment_condition_level,
  p.source, p.street,
  p.mf_reference_rent_czk, p.mf_gross_yield_pct,
  p.obec, p.okres, p.region,
  p.home_obec_pop, p.near_pop_5km, p.near_pop_15km, p.near_jobs_5km, p.near_jobs_15km,
  p.near_youth_5km, p.near_youth_15km, p.near_overall_5km, p.near_overall_15km,
  p.subtype, p.last_change_at,
  p.obec_id, p.okres_id, p.region_id,
  p.price_change_count, p.price_change_count_30d, p.price_change_count_90d,
  p.price_change_count_365d, p.total_price_change_pct,
  concat_ws(', '::text, p.street, p.locality) as place_search_text,
  p.asset_id
from properties p
where p.status = 'active'
  and (not (select publication_gate_enabled()) or p.published_at is not null);

comment on view browse_projection is
  'The ONE Browse read-model projection (migration 276): column contract + the '
  'publication-gate predicate in a single place. browse_list (5-min rebuild) and '
  'properties_map_mv (30-min rebuild, + lat/lng filter) are both materialized '
  'FROM this view by the rebuild functions in migration 277. The gate call MUST '
  'stay wrapped as (select publication_gate_enabled()) — a bare SECURITY DEFINER '
  'call cannot be inlined and runs per row (the PR-#707 incident); pinned by '
  'tests/test_browse_read_path_guardrail.py. Internal object: no anon grant.';

-------------------------------------------------------------------
-- 2. The list read model (empty shell; first rebuild fills it).
-------------------------------------------------------------------
-- LIMIT 0 keeps the migration instant; column types/order come from the
-- projection so the shell can never drift from it.

create unlogged table if not exists browse_list as
  select * from browse_projection limit 0;

-- 3. Lean index set. The rebuild recreates these on browse_list_next each
--    cycle (migration 277 keeps the list in lockstep — change them THERE too;
--    these exist so the empty shell is queryable and CI sees the contract).
--    Rationale for what's here and what is deliberately ABSENT:
--    - The table is physically ordered (category_main, category_type,
--      first_seen_at) at rebuild, so ANY within-category query reads a
--      CONTIGUOUS band (~25 MB worst case) — secondary sort lanes (last_seen,
--      area, price_per_m2, mf yield, ...) are served by a band scan + top-N
--      sort in well under the anon budget even cold, WITHOUT dedicated
--      indexes. Only two shapes genuinely need indexes beyond the PK:
--      the default lane's early-stop keyset (category + first_seen DESC) and
--      the district+price-sort lane (obec/okres/region rows are NOT contiguous
--      in a category+recency ordering — the migration-253 lesson).
--    - No geo covering index: bbox card lists ride the category band + lat/lng
--      filter (measured 11 ms post-#707); the MAP has its own clustered
--      matview.
create unique index if not exists browse_list_pk
  on browse_list (property_id);
create index if not exists browse_list_cat_first_seen_idx
  on browse_list (category_main, category_type, first_seen_at desc, property_id desc);
create index if not exists browse_list_obec_price_idx
  on browse_list (obec_id, category_type, price_czk) where obec_id is not null;
create index if not exists browse_list_okres_price_idx
  on browse_list (okres_id, category_type, price_czk) where okres_id is not null;
create index if not exists browse_list_region_price_idx
  on browse_list (region_id, category_type, price_czk) where region_id is not null;

grant select on browse_list to anon, authenticated;

comment on table browse_list is
  'Browse LIST read model (migration 276): every active property (incl. '
  'coordinate-less), a compact snapshot of browse_projection rebuilt blue-green '
  'every 5 min by rebuild_browse_list() (migration 277, pg_cron). UNLOGGED by '
  'design: no WAL for the 288 rebuilds/day; crash recovery truncates it and the '
  'next tick rebuilds (<=5 min gap, derived data only). Serves Browse '
  'cards/table/counts/stats; the map reads properties_map_mv; detail pages and '
  'the watchdog matcher stay on the live properties_public.';

-------------------------------------------------------------------
-- 4. Rebuild observability: one-row state, anon-readable view.
-------------------------------------------------------------------
-- The blue-green DROP+CREATE cycle destroys pg_stat history each swap, so the
-- map matview has NO last-refresh evidence today (a known gap). This tiny
-- state row is written by each rebuild function and is what the Health page
-- can age-check (pipeline_checks integration is a later, separate concern).

create table if not exists browse_read_model_state (
  id                smallint primary key default 1 check (id = 1),
  list_rebuilt_at   timestamptz,
  list_duration_ms  integer,
  list_rows         bigint,
  map_rebuilt_at    timestamptz,
  map_duration_ms   integer,
  map_rows          bigint
);
insert into browse_read_model_state (id) values (1) on conflict (id) do nothing;
alter table browse_read_model_state enable row level security;

create or replace view browse_read_model_state_public as
  select list_rebuilt_at, list_duration_ms, list_rows,
         map_rebuilt_at,  map_duration_ms,  map_rows
    from browse_read_model_state;
grant select on browse_read_model_state_public to anon, authenticated;

-------------------------------------------------------------------
-- 5. Street-denorm data fix (the COALESCE gap, ~4.2k rows).
-------------------------------------------------------------------
-- properties.street is supposed to be the group-best street (migration 183),
-- but ~4.2k active rows have p.street NULL while their repr listing carries
-- one — properties_public papers over it with COALESCE(p.street, l.street);
-- browse_list (bare p.street) would not. Fix the DENORM, not the projection:
-- enqueue the affected properties into the existing dirty queue so the 5-min
-- incremental maintenance recomputes their golden-record street through the
-- normal machinery. If a re-count days later shows the gap persists, that is
-- a hole in recompute's best_street logic — a separate upstream bug to fix,
-- not something to mask with a per-rebuild 450k-row listings join.

insert into dirty_properties (property_id)
select p.id
  from properties p
  join listings l on l.sreality_id = p.repr_listing_id
 where p.status = 'active'
   and p.street is null
   and l.street is not null
on conflict (property_id) do update set marked_at = now();
