-- 517_location_w6c_narrow_public_views.sql
--
-- W6-c of the location simplification sprint: "one slim store; the app gets
-- faster because there is less." Migration 508 (W4-c) re-sourced
-- `listings_public` onto `listing_location` but deliberately KEPT ITS WIDTH --
-- five materialized views hold an object-level dependency on the view, a
-- dependency that is on the VIEW and not on its columns, so `create or replace`
-- (which can only append) was the only shape available inside that wave's
-- window. This file takes the width.
--
-- THE CENSUS. Every column of both views was traced to its readers:
-- frontend/src (the `select=` lists, `lib/types.ts`, the filter registry), api/,
-- toolkit/, scripts/, location_data/, the five dependent matviews' own bodies
-- (via pg_depend's COLUMN-level refobjsubid, not by reading SQL), and every
-- function body in the catalog (`pg_get_functiondef ~ 'listings_public'` returns
-- nothing -- no RPC names either view).
--
--   listings_public: 61 -> 44 columns. The 44 that stay are EXACTLY
--   `DETAIL_COLS` in frontend/src/lib/queries.ts (the listing-detail read, four
--   call sites), of which api/notifications.py's `_LISTING_PROJECTION` reads 15,
--   api/curation.py reads 2 (`id`, `source`) and the five matviews read 8
--   (id, sreality_id, source, category_main, category_type, is_active,
--   first_seen_at, last_seen_at). The 17 that go had NO reader anywhere:
--     * four typed-NULL placeholders that project no data at all --
--       locality_district_id, locality_region_id (null::integer since 508),
--       broker_email, broker_phone (null::text since 398);
--     * ten legacy place columns the SPA stopped reading when migration 503
--       gave it the single `display_label` -- locality, district, street,
--       house_number, obec, okres, region, obec_id, okres_id, region_id;
--     * broker_name (the broker surfaces read broker_listings_public);
--     * building_condition_level, apartment_condition_level (Browse reads the
--       pair off browse_list, never off this view).
--   Ten of those seventeen were a join output off `listing_location`, so the
--   narrowed view also stops materializing them per row.
--
--   portal_listing_counts: 8 -> 8 columns, UNCHANGED, and that is the census
--   result rather than a deferral. All eight are read by its single reader,
--   portal_health_mv (migration 219 lines 58-75), so there is nothing to drop
--   and no reason to spend a rebuild on it. It is already minimal.
--
-- WHY BLUE-GREEN. A view's columns cannot be removed with `create or replace`,
-- and `drop view ... cascade` would take the five matviews down with it -- the
-- Health dashboard would read nothing until each was repopulated, and
-- image_storage_overview_mv scans the whole images mirror. So the wide view is
-- RENAMED out of the way first: the five matviews follow it by OID and keep
-- serving off `listings_public_legacy` while the narrow `listings_public` is
-- created under the original name and the SPA/PostgREST reads it immediately.
-- Each matview is then rebuilt beside itself as `<name>_next`, populated, and
-- swapped in with a drop+rename that holds ACCESS EXCLUSIVE for milliseconds.
-- The legacy view is dropped once nothing points at it. At no point is either
-- the listing-detail read or the Health dashboard without a relation to read.
--
-- THE REFRESH RACE. pg_cron's `refresh-health-dashboard` (*/10) calls
-- refresh_health_matviews(), which REFRESHes four of these five CONCURRENTLY;
-- scripts/refresh_image_stats.py refreshes the fifth. Neither takes an advisory
-- lock, so the swap can collide with one. It does not need to win: `set
-- lock_timeout = '5s'` turns a collision into a clean abort, every step of this
-- file is guarded by `if exists` / `if not exists`, and apply_migration.yml
-- re-runs the whole file on a lock timeout (30 attempts, 20s apart). A re-run
-- resumes from wherever it stopped -- it never re-drops a matview it has
-- already swapped, because the swap is gated on `<name>_next` still existing.
--
-- The matview bodies are NOT transcribed here. Each `_next` is built from
-- `pg_get_viewdef(<name>)` with the one relation name rewritten, so the
-- definition that comes out is the one that went in, character for character --
-- there is no chance of a hand-copied body drifting from the live one, and the
-- three body assertions in tests/test_health_image_matviews_listing_id.py
-- (which parse the LAST migration that creates each matview) keep pointing at
-- the migrations that really define them. Their unique indexes -- every one of
-- them is what makes REFRESH ... CONCURRENTLY legal -- are rebuilt the same way
-- off pg_indexes, and their ACLs are replayed from the source relation with
-- aclexplode rather than re-stated, so the `anon reads NOTHING` posture cannot
-- be undone by the schema's default privileges (which DO grant a fresh relation
-- to anon and authenticated).
--
-- Rule 25: net-negative. 17 columns leave, no column and no object arrives.
--
-- ci-allow-dynamic: <matview>_next — every CREATE MATERIALIZED VIEW, CREATE
-- UNIQUE INDEX and GRANT in sections 3 and 4 is built with EXECUTE, and that is
-- the point rather than a shortcut. The body comes from pg_get_viewdef and the
-- index from pg_indexes, so what is rebuilt is byte-for-byte what was there; the
-- grants are replayed from the source relation's own ACL through aclexplode, so
-- the `anon reads NOTHING` posture is copied rather than re-stated. It also
-- keeps the transient names out of apply_migration.yml's receipt, which probes
-- the live catalog for every object a migration's text DECLARES — a `_next`
-- spelled out here would be probed for after this same file renamed it away.

set lock_timeout = '5s';
set statement_timeout = '3600s';

-- ---------------------------------------------------------------------------
-- 1. Move the wide view aside. The five matviews follow the rename by OID and
--    go on serving; nothing else in the catalog names this view (checked with
--    pg_get_functiondef across every function in the database).
--
--    Guarded twice so a re-run is a no-op: the legacy name must be free, and
--    the view under the live name must still be the wide one (`display_label`
--    is the last column 508 appended, but `locality` is the one this file
--    removes -- testing for a column that GOES is what makes the guard false
--    on the second pass).
-- ---------------------------------------------------------------------------
do $$
begin
  if to_regclass('public.listings_public_legacy') is null
     and exists (
       select 1 from information_schema.columns
       where table_schema = 'public' and table_name = 'listings_public'
         and column_name = 'locality'
     )
  then
    execute 'alter view public.listings_public rename to listings_public_legacy';
  end if;
end
$$;

-- ---------------------------------------------------------------------------
-- 2. The narrow view, under the original name. 508's body minus the seventeen
--    columns of the census. `lat`/`lng`/`display_label` are read by the SPA, so
--    the left join to `listing_location` stays -- and keeps this view
--    non-auto-updatable, which is why `authenticated` holding SELECT on it can
--    never become a write path into `listings`.
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
  case
    when is_active then greatest(0, floor(extract(epoch from now() - first_seen_at) / 86400::numeric)::integer)
    else greatest(0, floor(extract(epoch from last_seen_at - first_seen_at) / 86400::numeric)::integer)
  end as tom_days,
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

-- Re-stated in full because this is a FRESH relation, not a replaced one: it
-- inherits the schema's default privileges, which grant a new view to anon and
-- authenticated alike. Ends exactly where 508 left the old one -- anon nothing,
-- authenticated SELECT, service_role everything.
revoke all on listings_public from public;
revoke all on listings_public from anon;
revoke all on listings_public from authenticated;
grant select on listings_public to authenticated;
grant all on listings_public to service_role;

-- ---------------------------------------------------------------------------
-- 3. Build each dependent matview beside itself, off the NARROW view.
--
--    `with no data` + REFRESH rather than `with data`, so a re-run that finds a
--    half-built `_next` finishes it instead of failing on the name. Index and
--    ACL are carried over from the relation being replaced.
-- ---------------------------------------------------------------------------
do $$
declare
  mv        text;
  nxt       text;
  body      text;
  idx       record;
  grantee   record;
begin
  if to_regclass('public.listings_public_legacy') is null then
    raise notice '517: no listings_public_legacy -- matview rebuild already done';
    return;
  end if;

  foreach mv in array array[
    'image_storage_overview_mv',
    'scraper_health_checks_mv',
    'health_summary_mv',
    'portal_health_mv',
    'category_trends_mv'
  ]
  loop
    nxt := mv || '_next';

    if to_regclass('public.' || mv) is null then
      raise notice '517: % absent -- skipping', mv;
      continue;
    end if;

    -- Already swapped by an earlier pass of this file (it is bound to the
    -- narrow view, not to the legacy one): nothing to rebuild, and rebuilding
    -- it anyway would repopulate a multi-million-row scan for nothing.
    if not exists (
      select 1 from pg_depend d
        join pg_rewrite r on r.oid = d.objid
        join pg_class dep on dep.oid = r.ev_class
       where dep.oid = ('public.' || mv)::regclass
         and d.refobjid = 'public.listings_public_legacy'::regclass
    ) then
      raise notice '517: % already rebuilt -- skipping', mv;
      continue;
    end if;

    if to_regclass('public.' || nxt) is null then
      body := pg_get_viewdef(('public.' || mv)::regclass, true);
      body := regexp_replace(body, '\mlistings_public_legacy\M', 'listings_public', 'g');
      body := rtrim(btrim(body), ';');
      if body ~ '\mlistings_public_legacy\M' then
        raise exception '517: % still names listings_public_legacy after rewrite', mv;
      end if;
      execute format('create materialized view public.%I as %s with no data', nxt, body);

      -- The ACL of the relation being replaced, replayed item by item. A fresh
      -- matview otherwise picks up the schema default (anon + authenticated),
      -- which tests/test_tenant_isolation_live.py fails on, correctly.
      execute format('revoke all on public.%I from public, anon, authenticated, service_role', nxt);
      for grantee in
        select case when a.grantee = 0 then 'public'
                    else a.grantee::regrole::text end as who,
               a.privilege_type as priv
          from pg_class c, aclexplode(c.relacl) a
         where c.oid = ('public.' || mv)::regclass
      loop
        execute format('grant %s on public.%I to %s', grantee.priv, nxt, grantee.who);
      end loop;
    end if;

    if not (select relispopulated from pg_class where oid = ('public.' || nxt)::regclass) then
      execute format('refresh materialized view public.%I', nxt);
    end if;

    -- Every one of these matviews carries exactly one UNIQUE index, and that is
    -- what makes the pg_cron REFRESH ... CONCURRENTLY legal. Rebuilt under a
    -- `_next` name (index names share one namespace with the old ones) and
    -- renamed back with the relation in section 4.
    for idx in
      select indexname, indexdef from pg_indexes
       where schemaname = 'public' and tablename = mv
    loop
      if to_regclass('public.' || idx.indexname || '_next') is null then
        execute replace(
                  replace(idx.indexdef,
                          'INDEX ' || idx.indexname || ' ON',
                          'INDEX ' || idx.indexname || '_next ON'),
                  ' ON public.' || mv || ' USING',
                  ' ON public.' || nxt || ' USING');
      end if;
    end loop;
  end loop;
end
$$;

-- ---------------------------------------------------------------------------
-- 4. The swap. One short transaction per matview: the lock is held only for the
--    catalog update, and `_next` still existing is what says "not yet swapped",
--    so a re-run cannot drop a matview it has already replaced.
-- ---------------------------------------------------------------------------
do $$
declare
  mv  text;
  nxt text;
  idx record;
begin
  foreach mv in array array[
    'image_storage_overview_mv',
    'scraper_health_checks_mv',
    'health_summary_mv',
    'portal_health_mv',
    'category_trends_mv'
  ]
  loop
    nxt := mv || '_next';
    continue when to_regclass('public.' || nxt) is null;

    execute format('drop materialized view if exists public.%I', mv);
    execute format('alter materialized view public.%I rename to %I', nxt, mv);

    for idx in
      select indexname from pg_indexes
       where schemaname = 'public' and tablename = mv and indexname like '%\_next'
    loop
      execute format('alter index public.%I rename to %I',
                     idx.indexname, left(idx.indexname, length(idx.indexname) - 5));
    end loop;
  end loop;
end
$$;

-- ---------------------------------------------------------------------------
-- 5. The wide view has no dependents left. Drop it.
-- ---------------------------------------------------------------------------
drop view if exists listings_public_legacy;

-- PostgREST caches the schema; the SPA reads this view through it.
select pg_notify('pgrst', 'reload schema');

-- ---------------------------------------------------------------------------
-- 6. Assert the end state rather than trusting it: the widths the census
--    measured, the seventeen names gone, five matviews present, populated,
--    uniquely indexed and bound to the NARROW view -- and no `_next` or
--    `_legacy` leftover anywhere.
-- ---------------------------------------------------------------------------
do $$
declare
  n_lp  integer;
  n_plc integer;
  stray text;
  mv    text;
begin
  select count(*) into n_lp from information_schema.columns
   where table_schema = 'public' and table_name = 'listings_public';
  if n_lp <> 44 then
    raise exception '517: listings_public has % columns, expected 44 (was 61)', n_lp;
  end if;

  select count(*) into n_plc from information_schema.columns
   where table_schema = 'public' and table_name = 'portal_listing_counts';
  if n_plc <> 8 then
    raise exception '517: portal_listing_counts has % columns, expected 8', n_plc;
  end if;

  select string_agg(column_name, ', ') into stray
    from information_schema.columns
   where table_schema = 'public' and table_name = 'listings_public'
     and column_name in ('locality', 'district', 'locality_district_id',
                         'locality_region_id', 'broker_name', 'broker_email',
                         'broker_phone', 'building_condition_level',
                         'apartment_condition_level', 'street', 'house_number',
                         'obec', 'okres', 'region', 'obec_id', 'okres_id',
                         'region_id');
  if stray is not null then
    raise exception '517: listings_public still projects %', stray;
  end if;

  if to_regclass('public.listings_public_legacy') is not null then
    raise exception '517: listings_public_legacy survived the swap';
  end if;

  foreach mv in array array[
    'image_storage_overview_mv',
    'scraper_health_checks_mv',
    'health_summary_mv',
    'portal_health_mv',
    'category_trends_mv'
  ]
  loop
    if to_regclass('public.' || mv) is null then
      raise exception '517: % is missing after the swap', mv;
    end if;
    if to_regclass('public.' || mv || '_next') is not null then
      raise exception '517: % survived the swap', mv || '_next';
    end if;
    if not (select relispopulated from pg_class where oid = ('public.' || mv)::regclass) then
      raise exception '517: % is not populated', mv;
    end if;
    if not exists (select 1 from pg_index i
                    where i.indrelid = ('public.' || mv)::regclass and i.indisunique) then
      raise exception '517: % lost its unique index -- REFRESH CONCURRENTLY would fail', mv;
    end if;
    if not exists (
      select 1 from pg_depend d
        join pg_rewrite r on r.oid = d.objid
        join pg_class dep on dep.oid = r.ev_class
       where dep.oid = ('public.' || mv)::regclass
         and d.refobjid = 'public.listings_public'::regclass
    ) then
      raise exception '517: % is not bound to listings_public', mv;
    end if;
  end loop;

  select string_agg(c.relname, ', ') into stray
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'public' and c.relname like '%\_next'
     and c.relkind in ('m', 'v', 'r');
  if stray is not null then
    raise exception '517: _next objects left behind: %', stray;
  end if;
end
$$;
