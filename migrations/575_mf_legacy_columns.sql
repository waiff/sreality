-- 575_mf_legacy_columns.sql -- the stored MF grains leave (MF program PR-F, the destructive
-- close-out). DESTRUCTIVE: applied only after the operator's explicit OK, and only after
-- backup_before_drop.yml has dumped every value it drops to R2 (CLAUDE.md rule 1; #1635).
--
-- WHY NOW. Since 567 every serving surface computes MF at read time: browse_projection and
-- properties_public call mf_reference() (565) over rent_map_cells, listing_feed_public reads the
-- property's number off the Browse read model (browse_list_mf), and PR-D deleted every writer.
-- The stored columns below have had no writer and no reader since; they are dead weight that a
-- future session would mistake for the source of truth.
--
-- THE CENSUS (live catalog, 2026-09-26: pg_depend/pg_rewrite column-level refobjsubid, every
-- function body, every trigger, index, constraint, policy and cron command; plus git grep):
--   listings.mf_reference_rent_czk / mf_gross_yield_pct / mf_reference_rent
--       one dependent: listings_public (517), which projects them with no reader left
--       (PR-C moved every reader to properties_public). Index listings_mf_gross_yield_pct_idx.
--   properties.mf_reference_rent_czk / mf_gross_yield_pct / mf_reference_rent
--       no view, trigger, constraint or policy. Indexes properties_mf_yield_keyset_idx (198)
--       and properties_cat_mf_yield_idx (231).
--   recompute_mf_gross_yields(), recompute_property_mf(bigint[]), ruian_katastr_code(geometry)
--       (507) -- called only by each other; no cron job, no view, no code.
--   rent_map_values_public, rent_map_adjustments_public (132)
--       no dependent at all: rent_map_choropleth reads the base tables, rent_map_cells (565)
--       replaced both as MF's input, and nothing in the repository reads either view.
--   ruian_admin_units.has_polygon (381) -- its writer left in PR-A; nothing reads it.
-- The functions that DO name mf_gross_yield_pct -- browse_list_mf, browse_map_cells,
-- browse_stats_properties -- read the read models' columns (browse_list, properties_map_mv),
-- which stay. So do the serving views' own MF columns.
--
-- WHY BLUE-GREEN FOR listings_public (517's recipe, re-run). A view's columns cannot be removed
-- with `create or replace`, and five matviews hold an object-level dependency on this view, so
-- `drop view ... cascade` would take the Health dashboard down until each repopulated. The wide
-- view is renamed aside (the matviews follow it by OID and keep serving), the narrow view takes
-- the name, each matview is rebuilt beside itself from its OWN pg_get_viewdef and swapped in,
-- and the wide view is dropped once nothing points at it. Unlike 517, the rename, the create and
-- the grants run in ONE transaction, so there is no instant without a `listings_public`.
--
-- LOCKS. The matview swap can collide with the */10 health refresh and the DROP COLUMNs need
-- ACCESS EXCLUSIVE on two hot tables; `lock_timeout` turns every collision into a clean abort
-- and apply_migration.yml re-runs the file (30 attempts). Both rebuild advisory locks
-- (522/535/566/567's preamble) are held ONLY around the base-table DDL, so a Browse rebuild never
-- holds the tables when the drop arrives, and the Browse read model is stale for seconds rather
-- than for the matview rebuilds.
--
-- IDEMPOTENT + STATEMENT AUTOCOMMIT: every step is guarded (`if exists`, `_next` still present,
-- the wide view still wide), so a retried file resumes where it stopped. The stored data is NOT
-- touched before the drop: the backup is what preserves it.
--
-- ROLLBACK: re-add the seven columns with their old types (integer, numeric, jsonb on both
-- tables; boolean not null default false on ruian_admin_units) and load the backup lane's CSVs
-- by id. The functions and views live in 507/132 and are not needed by anything that remains.
--
-- ci-allow-dynamic: <matview>_next -- 517's recipe: each matview body comes from its own
-- pg_get_viewdef, each unique index from pg_indexes and each ACL from aclexplode, so what is
-- rebuilt is byte-for-byte what was there, and no transient name reaches the apply receipt.

set lock_timeout = '5s';
set statement_timeout = '3600s';

-- ---------------------------------------------------------------------------
-- 0. Preconditions, stated as facts; a refusal is fixed at its cause, never overridden.
--    (a) 567 landed: the three serving views read MF at read time, not from a stored column.
--    (b) The doomed base columns have no view dependent other than listings_public (or the
--        wide copy this file parks as listings_public_legacy on a resumed run).
--    (c) The two rent-map views are dependent-free.
-- ---------------------------------------------------------------------------
do $$
declare
  bad text;
begin
  if not exists (select 1 from pg_depend d join pg_rewrite r on r.oid = d.objid
                  where r.ev_class = 'public.browse_projection'::regclass
                    and d.refobjid = 'public.mf_reference'::regproc)
     or not exists (select 1 from pg_depend d join pg_rewrite r on r.oid = d.objid
                     where r.ev_class = 'public.properties_public'::regclass
                       and d.refobjid = 'public.mf_reference'::regproc)
     or position('browse_list_mf(' in pg_get_viewdef('public.listing_feed_public'::regclass)) = 0
  then
    raise exception '575 refused: apply 567 first -- a serving view still reads stored MF';
  end if;

  select string_agg(distinct v.relname || ' <- ' || c.relname || '.' || a.attname, ', ')
    into bad
    from pg_class c
    join pg_attribute a on a.attrelid = c.oid and not a.attisdropped
    join pg_depend d on d.refobjid = c.oid and d.refobjsubid = a.attnum
                    and d.classid = 'pg_rewrite'::regclass
    join pg_rewrite r on r.oid = d.objid
    join pg_class v on v.oid = r.ev_class
   where c.relnamespace = 'public'::regnamespace
     and v.oid <> c.oid
     and v.relname not in ('listings_public', 'listings_public_legacy')
     and ((c.relname in ('listings', 'properties')
           and a.attname in ('mf_reference_rent_czk', 'mf_gross_yield_pct', 'mf_reference_rent'))
          or (c.relname = 'ruian_admin_units' and a.attname = 'has_polygon'));
  if bad is not null then
    raise exception '575 refused: a relation the census did not see reads a doomed column: %', bad;
  end if;

  select string_agg(distinct v.relname || ' <- ' || src.relname, ', ')
    into bad
    from pg_class src
    join pg_depend d on d.refobjid = src.oid and d.classid = 'pg_rewrite'::regclass
    join pg_rewrite r on r.oid = d.objid
    join pg_class v on v.oid = r.ev_class
   where src.relnamespace = 'public'::regnamespace
     and src.relname in ('rent_map_values_public', 'rent_map_adjustments_public')
     and v.oid <> src.oid;
  if bad is not null then
    raise exception '575 refused: a rent-map view has a dependent: %', bad;
  end if;
end
$$;

-- ---------------------------------------------------------------------------
-- 1. The narrow listings_public, in ONE transaction with the rename of the wide one. 517's body
--    minus its three MF columns: 41 columns, exactly DETAIL_COLS in frontend/src/lib/queries.ts.
--    The rename is guarded twice so a re-run is a no-op: the legacy name must be free and the
--    live view must still project a column this file removes.
-- ---------------------------------------------------------------------------
begin;

do $$
begin
  if to_regclass('public.listings_public_legacy') is null
     and exists (select 1 from pg_attribute
                  where attrelid = 'public.listings_public'::regclass
                    and attname = 'mf_reference_rent_czk' and not attisdropped)
  then
    execute 'alter view public.listings_public rename to listings_public_legacy';
  end if;
end
$$;

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

-- A FRESH relation inherits the schema's default privileges (anon + authenticated), so the
-- ACL is re-stated: anon nothing, authenticated SELECT, service_role everything -- 517's end state.
revoke all on listings_public from public;
revoke all on listings_public from anon;
revoke all on listings_public from authenticated;
grant select on listings_public to authenticated;
grant all on listings_public to service_role;

commit;

-- ---------------------------------------------------------------------------
-- 2. Build each dependent matview beside itself, off the NARROW view (517 §3 verbatim, but
--    for the name in its messages). `with no data` + REFRESH, so a re-run that finds a
--    half-built `_next` finishes it instead of failing on the name.
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
    raise notice '575: no listings_public_legacy -- matview rebuild already done';
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
      raise notice '575: % absent -- skipping', mv;
      continue;
    end if;

    if not exists (
      select 1 from pg_depend d
        join pg_rewrite r on r.oid = d.objid
        join pg_class dep on dep.oid = r.ev_class
       where dep.oid = ('public.' || mv)::regclass
         and d.refobjid = 'public.listings_public_legacy'::regclass
    ) then
      raise notice '575: % already rebuilt -- skipping', mv;
      continue;
    end if;

    if to_regclass('public.' || nxt) is null then
      body := pg_get_viewdef(('public.' || mv)::regclass, true);
      body := regexp_replace(body, '\mlistings_public_legacy\M', 'listings_public', 'g');
      body := rtrim(btrim(body), ';');
      if body ~ '\mlistings_public_legacy\M' then
        raise exception '575: % still names listings_public_legacy after rewrite', mv;
      end if;
      execute format('create materialized view public.%I as %s with no data', nxt, body);

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
-- 3. The swap (517 §4): one short transaction per matview, gated on `_next` still existing so
--    a re-run cannot drop a matview it has already replaced.
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

drop view if exists listings_public_legacy;

-- ---------------------------------------------------------------------------
-- 4. The drops. Both rebuild locks first: the acquire QUEUES behind an in-flight rebuild
--    (lock_timeout 0), and while they are held every pg_cron tick self-skips, so no rebuild
--    holds listings/properties when the ACCESS EXCLUSIVE arrives. The DDL itself fails fast.
-- ---------------------------------------------------------------------------
set statement_timeout = '900s';
set lock_timeout = 0;

select pg_advisory_lock(hashtext('rebuild_browse_list'));
select pg_advisory_lock(hashtext('rebuild_properties_map_mv'));

set lock_timeout = '5s';

-- The retired writers (507), callers of one another only.
drop function if exists public.recompute_mf_gross_yields();
drop function if exists public.recompute_property_mf(bigint[]);
drop function if exists public.ruian_katastr_code(geometry);

-- The retired MF inputs (132). RESTRICT, the default: a dependent the census missed fails here.
drop view if exists public.rent_map_values_public;
drop view if exists public.rent_map_adjustments_public;

-- The stored MF grains. Each table's indexes on them go first, by name, so the proof below
-- checks what was meant rather than what a column drop happened to take with it.
drop index if exists public.listings_mf_gross_yield_pct_idx;
alter table public.listings
  drop column if exists mf_reference_rent_czk,
  drop column if exists mf_gross_yield_pct,
  drop column if exists mf_reference_rent;

drop index if exists public.properties_mf_yield_keyset_idx;
drop index if exists public.properties_cat_mf_yield_idx;
alter table public.properties
  drop column if exists mf_reference_rent_czk,
  drop column if exists mf_gross_yield_pct,
  drop column if exists mf_reference_rent;

alter table public.ruian_admin_units drop column if exists has_polygon;

select pg_advisory_unlock(hashtext('rebuild_browse_list'));
select pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));

-- PostgREST caches the schema; the SPA reads listings_public through it.
select pg_notify('pgrst', 'reload schema');

-- ---------------------------------------------------------------------------
-- 5. Proof. Everything named above is gone; listings_public is 41 columns wide, anon-blind and
--    the five matviews are populated, uniquely indexed and bound to it; the three serving views
--    still publish MF and still read it at read time.
-- ---------------------------------------------------------------------------
do $$
declare
  stray   text;
  n_lp    integer;
  mv      text;
  missing text[] := '{}';
begin
  select string_agg(c.relname || '.' || a.attname, ', ') into stray
    from pg_attribute a join pg_class c on c.oid = a.attrelid
   where c.relnamespace = 'public'::regnamespace and a.attnum > 0 and not a.attisdropped
     and ((c.relname in ('listings', 'properties', 'listings_public')
           and a.attname in ('mf_reference_rent_czk', 'mf_gross_yield_pct', 'mf_reference_rent'))
          or (c.relname = 'ruian_admin_units' and a.attname = 'has_polygon'));
  if stray is not null then
    raise exception '575: columns survived: %', stray;
  end if;

  select string_agg(n, ', ') into stray
    from unnest(array['public.listings_mf_gross_yield_pct_idx',
                      'public.properties_mf_yield_keyset_idx',
                      'public.properties_cat_mf_yield_idx',
                      'public.rent_map_values_public',
                      'public.rent_map_adjustments_public']) n
   where to_regclass(n) is not null;
  if stray is not null then
    raise exception '575: relations survived: %', stray;
  end if;

  if to_regclass('public.listings_public_legacy') is not null then
    raise exception '575: listings_public_legacy survived the swap';
  end if;

  select string_agg(p.proname, ', ') into stray
    from pg_proc p
   where p.pronamespace = 'public'::regnamespace
     and p.proname in ('recompute_mf_gross_yields', 'recompute_property_mf', 'ruian_katastr_code');
  if stray is not null then
    raise exception '575: functions survived: %', stray;
  end if;

  select count(*) into n_lp from pg_attribute
   where attrelid = 'public.listings_public'::regclass and attnum > 0 and not attisdropped;
  if n_lp <> 41 then
    raise exception '575: listings_public has % columns, expected 41 (was 44)', n_lp;
  end if;
  if has_table_privilege('anon', 'public.listings_public', 'SELECT')
     or not has_table_privilege('authenticated', 'public.listings_public', 'SELECT') then
    raise exception '575: listings_public ACL is not anon-none / authenticated-select';
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
      raise exception '575: % is missing after the swap', mv;
    end if;
    if not (select relispopulated from pg_class where oid = ('public.' || mv)::regclass) then
      raise exception '575: % is not populated', mv;
    end if;
    if not exists (select 1 from pg_index i
                    where i.indrelid = ('public.' || mv)::regclass and i.indisunique) then
      raise exception '575: % lost its unique index -- REFRESH CONCURRENTLY would fail', mv;
    end if;
    if not exists (
      select 1 from pg_depend d
        join pg_rewrite r on r.oid = d.objid
       where r.ev_class = ('public.' || mv)::regclass
         and d.refobjid = 'public.listings_public'::regclass
    ) then
      raise exception '575: % is not bound to listings_public', mv;
    end if;
  end loop;

  select string_agg(c.relname, ', ') into stray
    from pg_class c
   where c.relnamespace = 'public'::regnamespace and c.relname like '%\_next'
     and c.relkind in ('m', 'v', 'r', 'i');
  if stray is not null then
    raise exception '575: _next objects left behind: %', stray;
  end if;

  if not exists (select 1 from pg_depend d join pg_rewrite r on r.oid = d.objid
                  where r.ev_class = 'public.browse_projection'::regclass
                    and d.refobjid = 'public.mf_reference'::regproc) then
    missing := missing || 'browse_projection no longer reads mf_reference()';
  end if;
  if not exists (select 1 from pg_depend d join pg_rewrite r on r.oid = d.objid
                  where r.ev_class = 'public.properties_public'::regclass
                    and d.refobjid = 'public.mf_reference'::regproc) then
    missing := missing || 'properties_public no longer reads mf_reference()';
  end if;
  if position('browse_list_mf(' in pg_get_viewdef('public.listing_feed_public'::regclass)) = 0 then
    missing := missing || 'listing_feed_public no longer reads browse_list_mf()';
  end if;
  select missing || array_agg(format('%s lost %s', v, c))
    into missing
    from (values ('browse_projection', 'mf_reference_rent_czk'),
                 ('browse_projection', 'mf_gross_yield_pct'),
                 ('properties_public', 'mf_reference_rent_czk'),
                 ('properties_public', 'mf_gross_yield_pct'),
                 ('properties_public', 'mf_reference_rent'),
                 ('listing_feed_public', 'mf_gross_yield_pct')) s(v, c)
   where not exists (select 1 from pg_attribute a
                      where a.attrelid = ('public.' || s.v)::regclass
                        and a.attname = s.c and not a.attisdropped);
  if array_length(missing, 1) is not null then
    raise exception '575: a serving view broke: %', array_to_string(missing, '; ');
  end if;
end
$$;

reset statement_timeout;
reset lock_timeout;
