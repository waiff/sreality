-- 615_merge_sprint_w6_drops.sql -- MERGE SPRINT W6, THE ONE DESTRUCTIVE WINDOW
-- (docs/design/merge-sprint/PROGRAM.md §6's table, §5's W6 gate, §7's brake). Rule 0 is met: since W1b,
-- W2a and W5 (#1717, #1719, #1723) no code on the API, the worker or the scheduled scrapers names
-- anything below.
--
-- ===========================================================================
-- DESTRUCTIVE. Applied only on the day the operator names, with their explicit OK, AFTER the R2
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
--   1. The per-ad lane's listings_portal_feed_idx (140 MB), CONCURRENTLY: its own top-level statement,
--      before the column it reads. Nothing reads its expression since W5 (the view and the function go in
--      section 3); its scans since (2026-10-08, hunted with a per-minute sampler) are the planner's PREFIX
--      choice for the broker lane's per-portal statements, `l.source = X AND l.id = ANY($1)`
--      (toolkit/broker_sources.py), on maxima / bezrealitky / mmreality / remax — listings_source_id_idx
--      (source, id) serves those at least as well, as it already does for the other five portals, and
--      section 0 refuses the apply if that index is missing.
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
--   5. Nothing: this section is gone. Migration 597 (portal exclusion, applied 2026-10-13) already
--      re-created browse_map_cells without listing_ids_filter, and 597 owns that function's body and
--      ACL. This file does not restate, pin or drop it; section 8 only calls it. Renumbered 593 -> 615 on
--      2026-10-17: 605's guard reads listing_feed_public, so the replay must run this file after 605.
--      Sections 6 to 8 keep their numbers.
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
  v_bad text;
  v_populated bool;
begin
  -- The two views this file restates are the ones it was written against (590's board, 377's card,
  -- read live 2026-10-08, identical on the CI replay), or this file's own card on a re-run.
  if md5(pg_get_viewdef('public.pipeline_board_public'::regclass)) <> '06c950df3e18e0f58c4be6b06973600e'
     or md5(pg_get_viewdef('public.property_pipeline_public'::regclass))
        not in ('54671707e2d2c12d1a47971325e9a784', '88093f6916bb54a0a9e4333fbf0cd544') then
    raise exception '615 refused: pipeline_board_public / property_pipeline_public are not 590''s / 377''s bodies';
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
    raise exception '615 refused: % carries a constraint or a dependant', v_bad;
  end if;

  -- Production only (the CI replay container has < 100k properties, no engine and no stats).
  select count(*) = 100000 into v_populated from (select 1 from public.properties limit 100000) probe;
  if not v_populated then
    raise notice '615: replay container (corpus < 100k properties), production preconditions skipped';
    return;
  end if;
  if coalesce((select value #>> '{}' from public.app_settings
                where key = 'realtime_autodedup_interval_seconds'), '0') <> '0' then
    raise exception '615 refused: the engine is not braked (realtime_autodedup_interval_seconds is not 0)';
  end if;
  if exists (select 1 from autodedup.rt_lease where expires_at > now()) then
    raise exception '615 refused: autodedup.rt_lease is still held; wait for it to expire';
  end if;
  if exists (select 1 from public.property_maintenance_lease
              where holder like 'full:%' and expires_at > now()) then
    raise exception '615 refused: the daily recompute holds the maintenance lease';
  end if;
  if to_regclass('public.listings_source_id_idx') is null then
    raise exception '615 refused: listings_source_id_idx is missing; it takes over the feed index''s prefix readers (the broker lane on four small portals)';
  end if;
  if exists (select 1 from pg_stat_user_indexes
              where indexrelname in ('properties_cat_last_seen_keyset_idx', 'properties_last_seen_keyset_idx')
                and idx_scan > 0) then
    raise exception '615 refused: a last-seen keyset index has been read: find the reader';
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
    raise notice '615: section 2 already applied';
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
    raise exception '615 refused: % depend(s) on the status log, the asset links or a write-only column', v_bad;
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
    raise notice '615: section 3 already applied';
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
    raise exception '615 refused: % depend(s) on the feed or on listings.discovery_seq', v_bad;
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
    raise notice '615: section 4 already applied';
    return;
  end if;
  if exists (select 1 from pg_attribute
              where attrelid = 'public.listings'::regclass and attname = 'discovery_seq' and not attisdropped) then
    raise exception '615 refused: listings.discovery_seq is still there (section 3 runs first)';
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
    raise exception '615 refused: % depend(s) on the queue''s discovery_seq or listing_discovery_seq', v_bad;
  end if;

  alter table public.listing_detail_queue drop column if exists discovery_seq;
  drop sequence if exists public.listing_discovery_seq;
end
$queue_seq$;

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
    raise notice '615: section 6 already applied';
    return;
  end if;
  select count(*) into v_cards from public.property_pipeline where note is not null;
  if v_cards > 0 then
    raise notice '615: property_pipeline.note KEPT, % card(s) hold a note (PROGRAM.md §5): a later migration drops it', v_cards;
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
    raise exception '615 refused: % depend(s) on the note or on the two pipeline views', v_bad;
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
-- 8. Post-conditions: every object gone, the restated two views as written, the readers answer.
-- ---------------------------------------------------------------------------
do $post$
declare
  v_left text;
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
    raise exception '615 did not land: still present: %', v_left;
  end if;

  if exists (select 1 from pg_attribute
              where attrelid = 'public.property_pipeline'::regclass and attname = 'note' and not attisdropped) then
    raise notice '615: property_pipeline.note kept (section 6 said why)';
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
    raise exception '615 did not land: the two pipeline views are not as written (body, invoker or grants)';
  end if;

  perform * from public.property_pipeline_public limit 1;
  perform * from public.pipeline_board_public limit 1;
  -- The map function is not this file's (the latest definition's, live and on the CI replay):
  -- the call shows the drops left its read whole.
  perform public.browse_map_cells(category_main_filter => array['byt'], category_type_filter => 'prodej',
                                  bbox_west => 14.40, bbox_south => 50.05, bbox_east => 14.45,
                                  bbox_north => 50.10, point_budget => 0);
  raise notice '615: landed';
end
$post$;

-- PostgREST re-reads the schema without the dropped relations and columns.
select pg_notify('pgrst', 'reload schema');

reset statement_timeout;
reset lock_timeout;
