-- 593_merge_sprint_w6_drops.sql -- MERGE SPRINT W6, THE ONE DESTRUCTIVE WINDOW
-- (docs/design/merge-sprint/PROGRAM.md §6's table, §5's W6 gate, §7's brake). Rule 0 is met: since W1b,
-- W2a and W5 (#1717, #1719, #1723) no code on the API, the worker or the scheduled scrapers names
-- anything below.
--
-- ===========================================================================
-- DESTRUCTIVE. Applied only on the day the operator names, with his explicit OK, AFTER the R2
-- backup has been read back (backup_before_drop.yml, label merge-sprint-w6: the status log and the
-- two asset tables whole; properties' three columns, listings.discovery_seq, the queue's
-- discovery_seq and the pipeline note as key + column CSVs). Forward only (rule 1); a restore is
-- that backup.
--
-- WINDOW: 05:20-05:30 UTC, after the 05:15 list rebuild and the 05:20 health refresh have ended,
-- with the engine braked (app_settings.realtime_autodedup_interval_seconds = 0 since 05:00 and its
-- lease expired; section 0 refuses otherwise). APPLY PATH: apply_migration.yml (psql -f,
-- statement autocommit, a plain 5 s lock limit, the workflow's 30 x 20 s retry re-runs the file):
-- every step is idempotent and a step already applied takes no lock on a re-run.
--
-- WHAT GOES (live pg_depend, 2026-10-08: nothing outside this list depends on any of it)
--   1. The per-ad lane's listings_portal_feed_idx (140 MB, frozen at 43,963 scans since W5 went live
--      2026-10-08 03:46 UTC), CONCURRENTLY: its own top-level statement, before the column it reads.
--   2. The status log (MS9): trigger properties_log_status_event, log_property_status_event(), the view
--      property_status_events_public, the table property_status_events (1.7 M rows, 215 MB). Asset links
--      (0 rows ever): asset_membership_events, properties.asset_id (its FK and partial index go with it),
--      assets. The write-only columns (written by nothing since W2a): properties.price_per_m2_source_listing_id
--      with price_per_m2_source_id(), properties.distinct_site_count. ONE transaction, properties locked
--      first (one ACCESS EXCLUSIVE acquisition; the drops are catalog-only).
--   3. The per-ad Browse lane: listing_feed_visible() (it returns the feed's row type), then the view
--      listing_feed_public, then listings.discovery_seq. The view is dropped before listings is locked:
--      a feed reader locks the view first.
--   4. listing_detail_queue.discovery_seq with its nextval default, then the sequence listing_discovery_seq
--      (after both columns; it is owned by neither).
--   5. browse_map_cells without listing_ids_filter (no caller has sent it since W5): DROP + CREATE in one
--      transaction (a parameter list cannot shrink in place), restated from the live body, whose
--      pg_get_functiondef md5 section 0 pins; the ACL re-issued as 537 did.
--   6. property_pipeline.note (0 of 133 cards hold one): SKIPPED with a notice if any card holds one; else
--      pipeline_board_public and property_pipeline_public are re-created without it first (outer view first,
--      the order a board read takes its locks), security_invoker and SELECT for authenticated only.
--   7. The never-used indexes (0 scans since creation; stats never reset), CONCURRENTLY:
--      properties_cat_last_seen_keyset_idx (652 MB), properties_last_seen_keyset_idx (576 MB).
--
-- NEVER HERE (PROGRAM.md §6, §7): curation tables, the pipeline history, all_sources / active_sources,
-- listings.published_at (listings_portal_feed_idx reads it; the column stays) and discovered_at, the AI
-- summary cache, our condition grades, and the dedup session's leave list (autodedup.*, the removed
-- engine's caches, property_merge_events.generation, properties.published_at / publish_reason with
-- properties_gate_cover_idx, which INCLUDEs published_at).
--
-- No CASCADE anywhere: each transaction opens with a check that refuses when anything outside its own
-- list depends on what it drops: a view or a row type (pg_depend); a function body (its source text,
-- which pg_depend does not track; for the note, also the trigger functions on its table); and what a
-- column's removal would take with it unasked: an index, or a constraint that also covers a column
-- that stays. Every other dependency makes the plain DROP refuse.
-- No ci-allow-dynamic marker: no EXECUTE builds anything.

set lock_timeout = '5s';
set statement_timeout = '120s';

-- ---------------------------------------------------------------------------
-- 0. Preconditions, before any lock.
-- ---------------------------------------------------------------------------
do $pre$
declare
  v_md5 text;
  v_bad text;
  v_populated bool;
begin
  -- The bodies this file restates are the ones it was written against (590's, read live 2026-10-08,
  -- identical on the CI replay), or this file's own on a re-run.
  if (select count(*) from pg_proc
       where pronamespace = 'public'::regnamespace and proname = 'browse_map_cells') <> 1 then
    raise exception '593 refused: public.browse_map_cells has more than one definition';
  end if;
  select md5(pg_get_functiondef(p.oid)) into v_md5 from pg_proc p
   where p.pronamespace = 'public'::regnamespace and p.proname = 'browse_map_cells';
  if v_md5 not in ('044d7fb46125b5d68dd3cf19dca31812', 'e46c6b45c2f73c81717af1c0fb3f39e3') then
    raise exception '593 refused: browse_map_cells (%) is neither 590''s definition nor this file''s. '
                    'Regenerate section 5 from the live body (the one edit: listing_ids_filter out)', v_md5;
  end if;
  if md5(pg_get_viewdef('public.pipeline_board_public'::regclass)) <> '06c950df3e18e0f58c4be6b06973600e'
     or md5(pg_get_viewdef('public.property_pipeline_public'::regclass))
        not in ('54671707e2d2c12d1a47971325e9a784', '88093f6916bb54a0a9e4333fbf0cd544') then
    raise exception '593 refused: pipeline_board_public / property_pipeline_public are not 590''s / 377''s bodies';
  end if;

  -- The three indexes are plain: no constraint stands on them.
  select string_agg(c.relname, ', ') into v_bad
    from pg_index i join pg_class c on c.oid = i.indexrelid
   where c.oid = any (array[to_regclass('public.properties_cat_last_seen_keyset_idx'),
                            to_regclass('public.properties_last_seen_keyset_idx'),
                            to_regclass('public.listings_portal_feed_idx')]::oid[])
     and (i.indisunique or i.indisprimary
          or exists (select 1 from pg_constraint k where k.conindid = i.indexrelid)
          or exists (select 1 from pg_depend d where d.refclassid = 'pg_class'::regclass
                                                and d.refobjid = i.indexrelid and d.deptype = 'n'));
  if v_bad is not null then
    raise exception '593 refused: % carries a constraint or a dependant', v_bad;
  end if;

  -- Production only (the CI replay container has < 100k properties, no engine and no stats).
  select count(*) = 100000 into v_populated from (select 1 from public.properties limit 100000) probe;
  if not v_populated then
    raise notice '593: replay container (corpus < 100k properties), production preconditions skipped';
    return;
  end if;
  if coalesce((select value #>> '{}' from public.app_settings
                where key = 'realtime_autodedup_interval_seconds'), '0') <> '0' then
    raise exception '593 refused: the engine is not braked (realtime_autodedup_interval_seconds is not 0)';
  end if;
  if exists (select 1 from autodedup.rt_lease where expires_at > now()) then
    raise exception '593 refused: autodedup.rt_lease is still held; wait for it to expire';
  end if;
  if exists (select 1 from public.property_maintenance_lease
              where holder like 'full:%' and expires_at > now()) then
    raise exception '593 refused: the daily recompute holds the maintenance lease';
  end if;
  if exists (select 1 from pg_stat_user_indexes
              where indexrelname = 'listings_portal_feed_idx'
                and last_idx_scan > timestamptz '2026-10-08 03:46:00+00') then
    raise exception '593 refused: listings_portal_feed_idx was read after W5 went live: find the reader';
  end if;
  if exists (select 1 from pg_stat_user_indexes
              where indexrelname in ('properties_cat_last_seen_keyset_idx', 'properties_last_seen_keyset_idx')
                and idx_scan > 0) then
    raise exception '593 refused: a last-seen keyset index has been read: find the reader';
  end if;
end
$pre$;

-- ---------------------------------------------------------------------------
-- 1. The feed index, CONCURRENTLY, before the column it reads (section 3): a top-level statement, its
--    own transaction. A run that times out after marking it invalid leaves it unread; the re-run
--    finishes it. The two keyset indexes go last (section 7): nothing in between needs them gone, and a
--    concurrent drop blocks no reader or writer, so they alone may finish after the window.
-- ---------------------------------------------------------------------------
drop index concurrently if exists public.listings_portal_feed_idx;

-- ---------------------------------------------------------------------------
-- 2. The status log, the asset links and the write-only columns: one transaction, properties first.
-- ---------------------------------------------------------------------------
do $status_assets$
declare
  v_cols int2[];
  v_bad text;
begin
  select coalesce(array_agg(attnum), '{}') into v_cols from pg_attribute
   where attrelid = 'public.properties'::regclass and not attisdropped
     and attname in ('asset_id', 'price_per_m2_source_listing_id', 'distinct_site_count');
  if v_cols = '{}'
     and to_regclass('public.property_status_events') is null
     and to_regclass('public.property_status_events_public') is null
     and to_regclass('public.asset_membership_events') is null
     and to_regclass('public.assets') is null
     and to_regprocedure('public.log_property_status_event()') is null
     and to_regprocedure('public.price_per_m2_source_id(numeric, numeric, bigint)') is null
     and not exists (select 1 from pg_trigger
                      where tgrelid = 'public.properties'::regclass and tgname = 'properties_log_status_event') then
    raise notice '593: section 2 already applied';
    return;
  end if;

  select string_agg(x, ', ') into v_bad from (
    -- a view or matview reading a dropped relation or column (other than the status log's own view)
    select distinct r.ev_class::regclass::text as x
      from pg_depend d join pg_rewrite r on r.oid = d.objid
     where d.classid = 'pg_rewrite'::regclass and d.refclassid = 'pg_class'::regclass
       and r.ev_class <> d.refobjid
       and r.ev_class is distinct from to_regclass('public.property_status_events_public')
       and (d.refobjid = any (array[to_regclass('public.property_status_events'),
                                    to_regclass('public.asset_membership_events'),
                                    to_regclass('public.assets')]::oid[])
            or (d.refobjid = 'public.properties'::regclass and d.refobjsubid = any (v_cols)))
    union all
    -- an index on one of the three columns, other than asset_id's own partial index
    select distinct d.objid::regclass::text
      from pg_depend d
     where d.classid = 'pg_class'::regclass and d.refclassid = 'pg_class'::regclass
       and d.refobjid = 'public.properties'::regclass and d.refobjsubid = any (v_cols)
       and d.objid is distinct from to_regclass('public.properties_asset_id_idx')
    union all
    -- a constraint over one of the three columns and a column that stays
    select k.conname::text
      from pg_constraint k
     where k.conrelid = 'public.properties'::regclass and k.conkey && v_cols and not (k.conkey <@ v_cols)
    union all
    -- a function whose body names a dropped object (pg_depend does not track a plpgsql or sql body)
    select p.oid::regprocedure::text
      from pg_proc p
     where p.pronamespace not in ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
       and p.prosrc ~* '(property_status_events|asset_membership_events|\massets\M|\masset_id\M|price_per_m2_source|distinct_site_count)'
       and p.oid is distinct from to_regprocedure('public.log_property_status_event()')
       and p.oid is distinct from to_regprocedure('public.price_per_m2_source_id(numeric, numeric, bigint)')
    union all
    -- anything on the two functions but the log's own trigger; anything typed on a dropped row type
    select pg_describe_object(d.classid, d.objid, d.objsubid)
      from pg_depend d
     where d.deptype = 'n'
       and ((d.refclassid = 'pg_proc'::regclass
             and d.refobjid = any (array[to_regprocedure('public.log_property_status_event()'),
                                         to_regprocedure('public.price_per_m2_source_id(numeric, numeric, bigint)')]::oid[])
             and d.objid is distinct from (select t.oid from pg_trigger t
                                            where t.tgrelid = 'public.properties'::regclass
                                              and t.tgname = 'properties_log_status_event'))
         or (d.refclassid = 'pg_type'::regclass
             and d.refobjid in (select c.reltype from pg_class c
                                 where c.oid = any (array[to_regclass('public.property_status_events'),
                                                          to_regclass('public.property_status_events_public'),
                                                          to_regclass('public.asset_membership_events'),
                                                          to_regclass('public.assets')]::oid[]))))
  ) deps;
  if v_bad is not null then
    raise exception '593 refused: % depend(s) on the status log, the asset links or a write-only column', v_bad;
  end if;

  lock table public.properties in access exclusive mode;
  drop trigger if exists properties_log_status_event on public.properties;
  drop function if exists public.log_property_status_event();
  drop view if exists public.property_status_events_public;
  drop table if exists public.property_status_events;
  drop table if exists public.asset_membership_events;
  alter table public.properties
    drop column if exists asset_id,
    drop column if exists price_per_m2_source_listing_id,
    drop column if exists distinct_site_count;
  drop table if exists public.assets;
  drop function if exists public.price_per_m2_source_id(numeric, numeric, bigint);
end
$status_assets$;

-- ---------------------------------------------------------------------------
-- 3. The per-ad Browse lane: the function, the view, then listings' column (the view before the table).
-- ---------------------------------------------------------------------------
do $feed$
declare
  v_col int2;
  v_bad text;
begin
  select attnum into v_col from pg_attribute
   where attrelid = 'public.listings'::regclass and attname = 'discovery_seq' and not attisdropped;
  if v_col is null
     and to_regprocedure('public.listing_feed_visible()') is null
     and to_regclass('public.listing_feed_public') is null then
    raise notice '593: section 3 already applied';
    return;
  end if;

  select string_agg(x, ', ') into v_bad from (
    -- a view reading the feed or the column (other than the feed itself)
    select distinct r.ev_class::regclass::text as x
      from pg_depend d join pg_rewrite r on r.oid = d.objid
     where d.classid = 'pg_rewrite'::regclass and d.refclassid = 'pg_class'::regclass
       and r.ev_class <> d.refobjid
       and r.ev_class is distinct from to_regclass('public.listing_feed_public')
       and (d.refobjid = to_regclass('public.listing_feed_public')
            or (d.refobjid = 'public.listings'::regclass and d.refobjsubid = v_col))
    union all
    -- an index still on the column (section 1 took the feed index)
    select distinct d.objid::regclass::text
      from pg_depend d
     where d.classid = 'pg_class'::regclass and d.refclassid = 'pg_class'::regclass
       and d.refobjid = 'public.listings'::regclass and d.refobjsubid = v_col
    union all
    -- a constraint over the column and a column that stays
    select k.conname::text
      from pg_constraint k
     where k.conrelid = 'public.listings'::regclass and k.conkey && array[v_col]
       and not (k.conkey <@ array[v_col])
    union all
    -- a function whose body names them, other than the feed's own
    select p.oid::regprocedure::text
      from pg_proc p
     where p.pronamespace not in ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
       and p.prosrc ~* '(listing_feed_public|listing_feed_visible|discovery_seq)'
       and p.oid is distinct from to_regprocedure('public.listing_feed_visible()')
    union all
    -- anything on the feed's function, or typed on the feed's row type but that function
    select pg_describe_object(d.classid, d.objid, d.objsubid)
      from pg_depend d
     where d.deptype = 'n'
       and ((d.refclassid = 'pg_proc'::regclass and d.refobjid = to_regprocedure('public.listing_feed_visible()'))
         or (d.refclassid = 'pg_type'::regclass
             and d.refobjid = (select c.reltype from pg_class c where c.oid = to_regclass('public.listing_feed_public'))
             and d.objid is distinct from to_regprocedure('public.listing_feed_visible()')))
  ) deps;
  if v_bad is not null then
    raise exception '593 refused: % depend(s) on the feed or on listings.discovery_seq', v_bad;
  end if;

  drop function if exists public.listing_feed_visible();
  drop view if exists public.listing_feed_public;
  alter table public.listings drop column if exists discovery_seq;
end
$feed$;

-- ---------------------------------------------------------------------------
-- 4. The queue's discovery_seq and its default, then the sequence (after both columns).
-- ---------------------------------------------------------------------------
do $queue_seq$
declare
  v_col int2;
  v_bad text;
begin
  select attnum into v_col from pg_attribute
   where attrelid = 'public.listing_detail_queue'::regclass and attname = 'discovery_seq' and not attisdropped;
  if v_col is null and to_regclass('public.listing_discovery_seq') is null then
    raise notice '593: section 4 already applied';
    return;
  end if;
  if exists (select 1 from pg_attribute
              where attrelid = 'public.listings'::regclass and attname = 'discovery_seq' and not attisdropped) then
    raise exception '593 refused: listings.discovery_seq is still there (section 3 runs first)';
  end if;

  select string_agg(x, ', ') into v_bad from (
    -- a view or an index on the queue's column
    select distinct pg_describe_object(d.classid, d.objid, d.objsubid) as x
      from pg_depend d
     where d.refclassid = 'pg_class'::regclass and d.refobjid = 'public.listing_detail_queue'::regclass
       and d.refobjsubid = v_col and d.classid in ('pg_rewrite'::regclass, 'pg_class'::regclass)
    union all
    -- a constraint over the column and a column that stays
    select k.conname::text
      from pg_constraint k
     where k.conrelid = 'public.listing_detail_queue'::regclass and k.conkey && array[v_col]
       and not (k.conkey <@ array[v_col])
    union all
    -- anything on the sequence but the queue column's own default
    select pg_describe_object(d.classid, d.objid, d.objsubid)
      from pg_depend d
     where d.refclassid = 'pg_class'::regclass and d.refobjid = to_regclass('public.listing_discovery_seq')
       and d.deptype = 'n'
       and d.objid is distinct from (select ad.oid from pg_attrdef ad
                                      where ad.adrelid = 'public.listing_detail_queue'::regclass
                                        and ad.adnum = v_col)
    union all
    -- a function whose body names either
    select p.oid::regprocedure::text
      from pg_proc p
     where p.pronamespace not in ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
       and p.prosrc ~* 'discovery_seq'
  ) deps;
  if v_bad is not null then
    raise exception '593 refused: % depend(s) on the queue''s discovery_seq or listing_discovery_seq', v_bad;
  end if;

  alter table public.listing_detail_queue drop column if exists discovery_seq;
  drop sequence if exists public.listing_discovery_seq;
end
$queue_seq$;

-- ---------------------------------------------------------------------------
-- 5. browse_map_cells without listing_ids_filter: 590's body less its one predicate, DROP + CREATE + the
--    ACL in one transaction (the parameter list cannot shrink in place).
-- ---------------------------------------------------------------------------
begin;

do $map_guard$
declare
  v_bad text;
begin
  select string_agg(pg_describe_object(d.classid, d.objid, d.objsubid), ', ') into v_bad
    from pg_depend d join pg_proc p on p.oid = d.refobjid
   where d.refclassid = 'pg_proc'::regclass and d.deptype = 'n'
     and p.pronamespace = 'public'::regnamespace and p.proname = 'browse_map_cells';
  if v_bad is not null then
    raise exception '593 refused: % depend(s) on browse_map_cells', v_bad;
  end if;
  select string_agg(p.oid::regprocedure::text, ', ') into v_bad
    from pg_proc p
   where p.pronamespace not in ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
     and p.proname <> 'browse_map_cells'
     and p.prosrc ~* '(browse_map_cells|listing_ids_filter)';
  if v_bad is not null then
    raise exception '593 refused: % call(s) browse_map_cells or name listing_ids_filter', v_bad;
  end if;
end
$map_guard$;

drop function if exists public.browse_map_cells(text[], text[], integer, integer, integer, integer, boolean,
  integer, integer, integer, integer, integer, integer, boolean, boolean, boolean, boolean, text[], boolean,
  boolean, boolean, integer, text[], bigint[], text[], text, double precision, double precision,
  double precision, double precision, text[], double precision, double precision, double precision,
  double precision, integer, double precision, double precision, text[], text[], jsonb, integer, integer,
  jsonb, double precision, double precision, text[], double precision, double precision, integer, integer,
  double precision, double precision, double precision, double precision, double precision,
  double precision, boolean[], text[], integer, integer, bigint[], text[], bigint[], integer, integer,
  integer, integer, integer, integer, double precision, boolean, boolean, bigint[], bigint[], integer,
  boolean);

create or replace function public.browse_map_cells(
  districts_filter text[] default null::text[],
  dispositions_filter text[] default null::text[],
  price_min_filter integer default null::integer,
  price_max_filter integer default null::integer,
  area_min_filter integer default null::integer,
  area_max_filter integer default null::integer,
  active_only_filter boolean default false,
  last_seen_min_days integer default null::integer,
  last_seen_max_days integer default null::integer,
  first_seen_min_days integer default null::integer,
  first_seen_max_days integer default null::integer,
  tom_days_min integer default null::integer,
  tom_days_max integer default null::integer,
  has_balcony_filter boolean default null::boolean,
  has_lift_filter boolean default null::boolean,
  has_parking_filter boolean default null::boolean,
  inactive_only_filter boolean default false,
  furnished_filter text[] default null::text[],
  terrace_filter boolean default null::boolean,
  cellar_filter boolean default null::boolean,
  garage_filter boolean default null::boolean,
  category_sub_cb_filter integer default null::integer,
  building_type_filter text[] default null::text[],
  tag_ids bigint[] default null::bigint[],
  category_main_filter text[] default null::text[],
  category_type_filter text default null::text,
  bbox_west double precision default null::double precision,
  bbox_south double precision default null::double precision,
  bbox_east double precision default null::double precision,
  bbox_north double precision default null::double precision,
  ownership_filter text[] default null::text[],
  estate_area_min_filter double precision default null::double precision,
  estate_area_max_filter double precision default null::double precision,
  usable_area_min_filter double precision default null::double precision,
  usable_area_max_filter double precision default null::double precision,
  parking_lots_min_filter integer default null::integer,
  garden_area_min_filter double precision default null::double precision,
  garden_area_max_filter double precision default null::double precision,
  condition_match_filter text[] default null::text[],
  districts_context_filter text[] default null::text[],
  city_index_rules jsonb default null::jsonb,
  city_pop_min integer default null::integer,
  city_pop_max integer default null::integer,
  city_proximity jsonb default null::jsonb,
  price_per_m2_min double precision default null::double precision,
  price_per_m2_max double precision default null::double precision,
  portal_filter text[] default null::text[],
  mf_gross_yield_pct_min double precision default null::double precision,
  mf_gross_yield_pct_max double precision default null::double precision,
  near_pop_5km_min integer default null::integer,
  near_pop_15km_min integer default null::integer,
  near_jobs_5km_min double precision default null::double precision,
  near_jobs_15km_min double precision default null::double precision,
  near_youth_5km_min double precision default null::double precision,
  near_youth_15km_min double precision default null::double precision,
  near_overall_5km_min double precision default null::double precision,
  near_overall_15km_min double precision default null::double precision,
  districts_excluded_filter boolean[] default null::boolean[],
  subtype_filter text[] default null::text[],
  recently_added_days integer default null::integer,
  recently_changed_days integer default null::integer,
  obec_ids_filter bigint[] default null::bigint[],
  districts_levels text[] default null::text[],
  districts_ids bigint[] default null::bigint[],
  building_condition_level_min integer default null::integer,
  building_condition_level_max integer default null::integer,
  apartment_condition_level_min integer default null::integer,
  apartment_condition_level_max integer default null::integer,
  price_change_count_min integer default null::integer,
  price_change_window_days integer default null::integer,
  total_price_change_pct_filter double precision default null::double precision,
  with_estimates boolean default false,
  include_no_price boolean default false,
  property_ids_filter bigint[] default null::bigint[],
  -- The one parameter browse_stats_properties does not have.
  -- Above this many mappable rows the answer is cells; at or below it the
  -- caller re-reads the cohort as points. Measured: a 2,000-row point read is
  -- 153 blocks and ~906 KB, which is the ceiling this threshold buys.
  point_budget integer default 2000,
  -- migration 537: exclude the caller's dismissed properties (RLS-scoped).
  hide_dismissed boolean default false
)
returns jsonb
language plpgsql
stable
-- 74 mostly-NULL parameters: a generic plan would pick one shape and use it for
-- every cohort. Same setting, same reason, as browse_stats_properties.
set plan_cache_mode to 'force_custom_plan'
as $function$
declare
  -- The Czech Republic, used only when the cohort carries no bbox of its own.
  -- Real CZ bounds are lng 12.09..18.86, lat 48.55..51.06.
  c_cz_west  constant double precision := 12.0;
  c_cz_east  constant double precision := 18.9;
  c_cz_south constant double precision := 48.5;
  c_cz_north constant double precision := 51.1;
  c_cols constant integer := 20;
  c_rows constant integer := 13;
  v_w double precision;
  v_e double precision;
  v_s double precision;
  v_n double precision;
  v_cw double precision;
  v_ch double precision;
  v_result jsonb;
  v_total bigint;
begin
  if city_proximity is not null then
    raise exception using errcode = '22023',
      message = 'browse_map_cells: city_proximity is retired (W5, migration 436). '
                'Use the migration-142 near_*_min columns.';
  end if;

  v_w := coalesce(bbox_west,  c_cz_west);
  v_e := coalesce(bbox_east,  c_cz_east);
  v_s := coalesce(bbox_south, c_cz_south);
  v_n := coalesce(bbox_north, c_cz_north);
  -- A degenerate extent (a point bbox, or an inverted one from a bad URL) would
  -- divide by zero or produce a negative cell; fall back to the CZ box rather
  -- than raise, because the cohort predicate is still perfectly well defined.
  if not (v_e > v_w and v_n > v_s) then
    v_w := c_cz_west; v_e := c_cz_east; v_s := c_cz_south; v_n := c_cz_north;
  end if;
  v_cw := (v_e - v_w) / c_cols;
  v_ch := (v_n - v_s) / c_rows;

  with g as (
    select
      -- cx/cy are NULL together for a row outside the grid extent. Those rows
      -- collapse into ONE group, are reported as off_grid, and are still inside
      -- total -- never relocated onto an edge cell, never dropped.
      case when l.lat >= v_s and l.lat <= v_n and l.lng >= v_w and l.lng <= v_e
           then least(floor((l.lng - v_w) / v_cw)::int, c_cols - 1) end as cx,
      case when l.lat >= v_s and l.lat <= v_n and l.lng >= v_w and l.lng <= v_e
           then least(floor((l.lat - v_s) / v_ch)::int, c_rows - 1) end as cy,
      count(*)::int as n,
      -- The MEAN of the points in the cell, not the cell centre: the bubble
      -- then sits on the settlement instead of on a lattice vertex. Both
      -- columns are index keys, so this costs nothing and keeps Heap Fetches 0.
      avg(l.lat) as la,
      avg(l.lng) as lo
    from properties_map_mv l
    where
          public.portal_status_matches(l.is_active, l.all_sources, l.active_sources, portal_filter,
            case when active_only_filter then 'active' when inactive_only_filter then 'inactive' else 'any' end)
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
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)])),
                 coalesce(districts_levels, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_ids, array_fill(null::bigint, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, excl, lvl, admin_id, ord)
          where not coalesce(excl, false)
            and case
              -- ONE code predicate (W3 S3): a chip is a level plus a RUIAN code,
              -- matched with plain equality. No ILIKE, no place_search_text, no
              -- context narrow. A chip with no code matches NOTHING (the `else`
              -- and the null guard) -- a saved filter we cannot resolve must
              -- never widen a cohort.
              when admin_id is null                  then false
              when lvl = 'kraj'                      then l.region_id    = admin_id
              when lvl = 'okres'                     then l.okres_id     = admin_id
              when lvl = 'obec'                      then l.obec_id      = admin_id
              when lvl = 'cast_obce'                 then l.cast_obce_id = admin_id
              -- a street / POI / address pick carries its containing obec code
              when lvl = 'locality'                  then l.obec_id      = admin_id
              else false
            end
        )
      )
      and (
        districts_filter is null or array_length(districts_filter, 1) is null
        or not exists (
          select 1 from unnest(districts_filter,
                 coalesce(districts_excluded_filter, array_fill(false, array[array_length(districts_filter, 1)])),
                 coalesce(districts_levels, array_fill(null::text, array[array_length(districts_filter, 1)])),
                 coalesce(districts_ids, array_fill(null::bigint, array[array_length(districts_filter, 1)]))
               ) with ordinality as t(needle, excl, lvl, admin_id, ord)
          where coalesce(excl, false)
            and case
              -- ONE code predicate (W3 S3): a chip is a level plus a RUIAN code,
              -- matched with plain equality. No ILIKE, no place_search_text, no
              -- context narrow. A chip with no code matches NOTHING (the `else`
              -- and the null guard) -- a saved filter we cannot resolve must
              -- never widen a cohort.
              when admin_id is null                  then false
              when lvl = 'kraj'                      then l.region_id    = admin_id
              when lvl = 'okres'                     then l.okres_id     = admin_id
              when lvl = 'obec'                      then l.obec_id      = admin_id
              when lvl = 'cast_obce'                 then l.cast_obce_id = admin_id
              -- a street / POI / address pick carries its containing obec code
              when lvl = 'locality'                  then l.obec_id      = admin_id
              else false
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
      and (
        ownership_filter is null or array_length(ownership_filter, 1) is null
        or l.ownership = any(ownership_filter)
        or ('__unknown__' = any(ownership_filter)
            and (l.ownership is null or not (l.ownership = any(array['osobni','druzstevni','statni','jine']))))
      )
      and (estate_area_min_filter  is null or l.plot_area_m2  >= estate_area_min_filter)
      and (estate_area_max_filter  is null or l.plot_area_m2  <= estate_area_max_filter)
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
      and (not hide_dismissed or not exists (
            select 1 from property_dismissals_public d where d.property_id = l.property_id))
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
      and (city_index_rules is null or jsonb_array_length(city_index_rules) = 0
           or l.obec_id = any (array(select curated_cities_matching(city_index_rules))))
      and true
    group by 1, 2
  )
  select jsonb_build_object(
    'total',        coalesce((select sum(n) from g), 0),
    'off_grid',     coalesce((select sum(n) from g where cx is null), 0),
    'cell_lat_deg', v_ch,
    'cell_lng_deg', v_cw,
    'grid_west',  v_w, 'grid_east',  v_e,
    'grid_south', v_s, 'grid_north', v_n,
    'cells', coalesce(
      (select jsonb_agg(jsonb_build_object('lat', la, 'lng', lo, 'n', n) order by n desc)
         from g where cx is not null),
      '[]'::jsonb)
  )
  into v_result;

  v_total := (v_result->>'total')::bigint;
  -- At or below the budget the caller re-reads the cohort as points, so the
  -- cells are withheld rather than shipped-and-ignored. `clustered` is what the
  -- caller branches on; it is never inferred from the array being empty (an
  -- empty cohort is not a small one).
  return v_result || jsonb_build_object(
    'clustered', v_total > point_budget,
    'cells', case when v_total > point_budget then v_result->'cells' else 'null'::jsonb end
  );
end
$function$;

revoke execute on function public.browse_map_cells(text[], text[], integer, integer, integer, integer, boolean,
  integer, integer, integer, integer, integer, integer, boolean, boolean, boolean, boolean, text[], boolean,
  boolean, boolean, integer, text[], bigint[], text[], text, double precision, double precision,
  double precision, double precision, text[], double precision, double precision, double precision,
  double precision, integer, double precision, double precision, text[], text[], jsonb, integer, integer,
  jsonb, double precision, double precision, text[], double precision, double precision, integer, integer,
  double precision, double precision, double precision, double precision, double precision,
  double precision, boolean[], text[], integer, integer, bigint[], text[], bigint[], integer, integer,
  integer, integer, integer, integer, double precision, boolean, boolean, bigint[], integer, boolean)
  from public, anon;
grant execute on function public.browse_map_cells(text[], text[], integer, integer, integer, integer, boolean,
  integer, integer, integer, integer, integer, integer, boolean, boolean, boolean, boolean, text[], boolean,
  boolean, boolean, integer, text[], bigint[], text[], text, double precision, double precision,
  double precision, double precision, text[], double precision, double precision, double precision,
  double precision, integer, double precision, double precision, text[], text[], jsonb, integer, integer,
  jsonb, double precision, double precision, text[], double precision, double precision, integer, integer,
  double precision, double precision, double precision, double precision, double precision,
  double precision, boolean[], text[], integer, integer, bigint[], text[], bigint[], integer, integer,
  integer, integer, integer, integer, double precision, boolean, boolean, bigint[], integer, boolean)
  to authenticated, service_role;

commit;

-- ---------------------------------------------------------------------------
-- 6. The pipeline note: skipped with a notice when a card holds one; else the two views re-created
--    without it (outer view first), then the column. One transaction.
-- ---------------------------------------------------------------------------
do $note$
declare
  v_bad text;
  v_cards bigint;
  v_note int2;
begin
  select attnum into v_note from pg_attribute
   where attrelid = 'public.property_pipeline'::regclass and attname = 'note' and not attisdropped;
  if v_note is null then
    raise notice '593: section 6 already applied';
    return;
  end if;
  select count(*) into v_cards from public.property_pipeline where note is not null;
  if v_cards > 0 then
    raise notice '593: property_pipeline.note KEPT, % card(s) hold a note (PROGRAM.md §5): a later migration drops it', v_cards;
    return;
  end if;

  -- Nothing but the card view reads the column, nothing but the board reads the card view, nothing reads
  -- the board; no index, wider constraint or function body needs the column.
  select string_agg(x, ', ') into v_bad from (
    select distinct r.ev_class::regclass::text as x
      from pg_depend d join pg_rewrite r on r.oid = d.objid
     where d.classid = 'pg_rewrite'::regclass and d.refclassid = 'pg_class'::regclass
       and r.ev_class <> d.refobjid
       and ((d.refobjid = 'public.property_pipeline'::regclass and d.refobjsubid = v_note
             and r.ev_class <> 'public.property_pipeline_public'::regclass)
         or (d.refobjid = 'public.property_pipeline_public'::regclass
             and r.ev_class <> 'public.pipeline_board_public'::regclass)
         or d.refobjid = 'public.pipeline_board_public'::regclass)
    union all
    select pg_describe_object(d.classid, d.objid, d.objsubid)
      from pg_depend d
     where d.refclassid = 'pg_type'::regclass and d.deptype = 'n'
       and d.refobjid in (select c.reltype from pg_class c
                           where c.oid in ('public.property_pipeline_public'::regclass,
                                           'public.pipeline_board_public'::regclass))
    union all
    -- an index on the note
    select distinct d.objid::regclass::text
      from pg_depend d
     where d.classid = 'pg_class'::regclass and d.refclassid = 'pg_class'::regclass
       and d.refobjid = 'public.property_pipeline'::regclass and d.refobjsubid = v_note
    union all
    -- a constraint over the note and a column that stays (the note's own check goes with it)
    select k.conname::text
      from pg_constraint k
     where k.conrelid = 'public.property_pipeline'::regclass and k.conkey && array[v_note]
       and not (k.conkey <@ array[v_note])
    union all
    -- a function naming either view, or reading the note by name: from the table, or as NEW / OLD in
    -- a trigger on it
    select p.oid::regprocedure::text
      from pg_proc p
     where p.pronamespace not in ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
       and (p.prosrc ~* '(property_pipeline_public|pipeline_board_public)'
            or (p.prosrc ~* '\mnote\M'
                and (p.prosrc ~* '\mproperty_pipeline\M'
                     or p.oid in (select t.tgfoid from pg_trigger t
                                   where t.tgrelid = 'public.property_pipeline'::regclass))))
  ) deps;
  if v_bad is not null then
    raise exception '593 refused: % depend(s) on the note or on the two pipeline views', v_bad;
  end if;

  drop view public.pipeline_board_public;
  drop view public.property_pipeline_public;

  -- 377's body less `pp.note`.
  create view property_pipeline_public
  with (security_invoker = true) as
  select pp.property_id, pp.stage_id, ps.key as stage_key, ps.label as stage_label,
         ps.position as stage_position, ps.color as stage_color, ps.is_terminal,
         pp.board_position, pp.entered_stage_at, pp.added_at, pp.updated_at,
         ps.code as stage_code
  from property_pipeline pp
  join pipeline_stages ps on ps.id = pp.stage_id;

  revoke all on public.property_pipeline_public from public, anon, authenticated;
  grant select on public.property_pipeline_public to authenticated;

  -- 590's body, verbatim.
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
    p.display_label,
    p.cast_obce_id,
    p.ulice_id
  from property_pipeline_public pp
  left join properties_public p on p.property_id = pp.property_id;

  revoke all on public.pipeline_board_public from public, anon, authenticated;
  grant select on public.pipeline_board_public to authenticated;

  alter table public.property_pipeline drop column note;
end
$note$;

-- ---------------------------------------------------------------------------
-- 7. The never-used keyset indexes, CONCURRENTLY (top-level statements; safe after the window too).
-- ---------------------------------------------------------------------------
drop index concurrently if exists public.properties_cat_last_seen_keyset_idx;
drop index concurrently if exists public.properties_last_seen_keyset_idx;

-- ---------------------------------------------------------------------------
-- 8. Post-conditions: every object gone, the restated three as written, the readers answer.
-- ---------------------------------------------------------------------------
do $post$
declare
  v_left text;
  v_map oid;
begin
  select string_agg(x, ', ') into v_left from (
    select 'relation ' || n as x
      from unnest(array['public.property_status_events', 'public.property_status_events_public',
                        'public.asset_membership_events', 'public.assets', 'public.listing_feed_public',
                        'public.listing_discovery_seq', 'public.listings_portal_feed_idx',
                        'public.properties_cat_last_seen_keyset_idx',
                        'public.properties_last_seen_keyset_idx']) n
     where to_regclass(n) is not null
    union all
    select 'function ' || f
      from unnest(array['public.log_property_status_event()', 'public.listing_feed_visible()',
                        'public.price_per_m2_source_id(numeric, numeric, bigint)']) f
     where to_regprocedure(f) is not null
    union all
    select 'trigger properties_log_status_event' from pg_trigger
     where tgrelid = 'public.properties'::regclass and tgname = 'properties_log_status_event'
    union all
    select 'column ' || attrelid::regclass::text || '.' || attname from pg_attribute
     where not attisdropped
       and ((attrelid = 'public.properties'::regclass
             and attname in ('asset_id', 'price_per_m2_source_listing_id', 'distinct_site_count'))
         or (attrelid in ('public.listings'::regclass, 'public.listing_detail_queue'::regclass)
             and attname = 'discovery_seq'))
  ) still;
  if v_left is not null then
    raise exception '593 did not land: still present: %', v_left;
  end if;

  select p.oid into v_map from pg_proc p
   where p.pronamespace = 'public'::regnamespace and p.proname = 'browse_map_cells';
  if (select count(*) from pg_proc where pronamespace = 'public'::regnamespace and proname = 'browse_map_cells') <> 1
     or (select md5(prosrc) from pg_proc where oid = v_map) is distinct from 'd60bf1ec13b86d3a7afeb0dee0695c26'
     or (select md5(pg_get_functiondef(v_map))) is distinct from 'e46c6b45c2f73c81717af1c0fb3f39e3'
     or has_function_privilege('anon', v_map, 'execute')
     or not has_function_privilege('authenticated', v_map, 'execute')
     or not has_function_privilege('service_role', v_map, 'execute') then
    raise exception '593 did not land: browse_map_cells is not this file''s definition with its grants';
  end if;

  if exists (select 1 from pg_attribute
              where attrelid = 'public.property_pipeline'::regclass and attname = 'note' and not attisdropped) then
    raise notice '593: property_pipeline.note kept (section 6 said why)';
  elsif md5(pg_get_viewdef('public.property_pipeline_public'::regclass)) is distinct from '88093f6916bb54a0a9e4333fbf0cd544'
     or md5(pg_get_viewdef('public.pipeline_board_public'::regclass)) is distinct from '06c950df3e18e0f58c4be6b06973600e'
     or not (select coalesce(reloptions, '{}') @> array['security_invoker=true'] from pg_class
              where oid = 'public.property_pipeline_public'::regclass)
     or not (select coalesce(reloptions, '{}') @> array['security_invoker=true'] from pg_class
              where oid = 'public.pipeline_board_public'::regclass)
     or has_table_privilege('anon', 'public.property_pipeline_public', 'select')
     or has_table_privilege('anon', 'public.pipeline_board_public', 'select')
     or not has_table_privilege('authenticated', 'public.property_pipeline_public', 'select')
     or not has_table_privilege('authenticated', 'public.pipeline_board_public', 'select')
     or has_table_privilege('authenticated', 'public.property_pipeline_public', 'insert,update,delete,truncate')
     or has_table_privilege('authenticated', 'public.pipeline_board_public', 'insert,update,delete,truncate') then
    raise exception '593 did not land: the two pipeline views are not as written (body, invoker or grants)';
  end if;

  perform * from public.property_pipeline_public limit 1;
  perform * from public.pipeline_board_public limit 1;
  perform public.browse_map_cells(category_main_filter => array['byt'], category_type_filter => 'prodej',
                                  bbox_west => 14.40, bbox_south => 50.05, bbox_east => 14.45,
                                  bbox_north => 50.10, point_budget => 0);
  raise notice '593: landed';
end
$post$;

-- PostgREST re-reads the dropped relations and the map RPC's new parameter list.
select pg_notify('pgrst', 'reload schema');

reset statement_timeout;
reset lock_timeout;
