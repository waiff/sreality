-- 584_serve_street_code.sql -- the street code is SERVED: `ulice_id` (= listing_location.ulice_kod)
-- appended LAST to the five relations a location chip filters, and so to both read models.
--
-- WHY. Operator ruling 2026-10-02, Q1 (rule 25: "a field is added only by operator ruling"): the
-- street becomes the fifth chip level (kraj, okres, obec, cast_obce, ulice), so the RÚIAN street
-- code every resolved listing already carries in `listing_location.ulice_kod` (migration 501;
-- `street_name IS NOT NULL <=> ulice_kod IS NOT NULL` since resolver W18) must reach the relations
-- the filters read: browse_projection (-> browse_list, properties_map_mv), properties_public (the
-- Watchdog matcher's alias `l`), pipeline_board_public (the kanban) and listing_feed_public (the
-- portal-mirror lane). Same name and type as its four siblings: `<level>_id bigint`.
--
-- ZERO BEHAVIOUR CHANGE. Nothing reads the column yet: no RPC arm, no index, no code. The chip
-- arm (`when lvl = 'ulice' then l.ulice_id = admin_id`) and any index are the NEXT migration,
-- after the read paths are measured against this one.
--
-- APPEND-ONLY, BY POSTGRES AND BY THE POSITIONAL INSERT. `create or replace view` refuses to
-- move an existing column, and `toolkit/browse_read_model.sync_browse_list` patches browse_list
-- with `INSERT INTO browse_list SELECT * FROM browse_projection` -- by POSITION. Each body below
-- is its latest definer VERBATIM (567 for the three 567 swapped, 508 for the kanban) with one
-- line appended; tests/test_location_w3_projection.py holds the prefix.
--
-- THE ORDER, and why each step sits where it does (522/535/561/566/567's recipe):
--   0. Preconditions, before any lock or DDL: the two dismissal-aware map/list sources and the
--      two rebuild functions steps 4-5 force are the bodies this file was written against (md5
--      of prosrc, as read live 2026-10-02).
--   1. Both rebuild advisory locks, queued (lock_timeout 0) behind an in-flight rebuild; from
--      then on every pg_cron tick self-skips in milliseconds and the DDL is uncontended. The
--      wait runs under a 1900 s statement budget: a pg_cron list rebuild may hold its key up to
--      its own 1800 s cap, and apply_migration.yml retries a LOCK timeout only, never a
--      statement timeout. Then fail fast (5 s, the hot-table rule) -- the workflow re-runs the
--      file on a lock timeout and every statement is idempotent.
--   2. TX A: properties_public, pipeline_board_public (reads properties_public, so after it),
--      listing_feed_public. No read model is involved; readers see the column at COMMIT.
--   3. TX B: `browse_list` gains the column FIRST, then browse_projection. Two reasons. (a)
--      `sync_browse_list`'s positional insert and `browse_list_visible()` (`select l.*`,
--      `returns setof browse_projection`) must never see the table one column narrower than
--      the view -- after this TX both are 64 wide, the table's new column NULL until the
--      rebuild in step 5 fills it. (b) It is the lock order `sync_browse_list` itself takes
--      (browse_list, then the projection), so the swap cannot deadlock against a patch.
--      `properties_map_mv` is a materialized view and cannot be altered, so
--      `properties_map_visible()` (`select m.*`, `returns setof browse_projection`) becomes a
--      BRIDGE in the same TX: 561's body plus `null::bigint as ulice_id`. It exists only
--      until the map is rebuilt below, and only while the map is narrow (a re-run after the
--      rebuild leaves 561's body alone -- `m.*` plus the null would then be 65 wide).
--   4. The map rebuilt by the function pg_cron runs (this session holds its advisory key, which
--      is re-entrant, so it RUNS rather than skips), then 561's body restored -- one TX, so the
--      wide map and the bridge-free source publish together.
--   5. The list rebuilt the same way: `rebuild_browse_list()` drops and re-creates browse_list
--      from the projection, so `ulice_id` is filled from the one definition and the 522
--      indexes are rebuilt with it. lock_timeout 0 for both (522 §4: a lock_timeout would
--      throw away a finished rebuild at its final rename).
--   6. Post-conditions, the locks still held; 7. unlock, PostgREST schema reload.
--
-- APPLY OFF-HOURS via apply_migration.yml: the map serves NULL streets for its rebuild and
-- Browse waits one list rebuild for a street code it does not read yet (pg_cron, 24 h to
-- 2026-10-03: map p50 204 s, max 316 s; list p50 256 s, but 20-28 min under daytime load and
-- one run cancelled at its 1800 s cap). The job's own cap is 60 minutes, which one queued
-- lock wait plus a slow forced rebuild can exceed: dispatch right after a list rebuild ends.
-- ROLLBACK: none needed (additive); removing a view column is DROP + CREATE.
--
-- ci-allow-dynamic: properties_map_visible -- step 3's bridge is created only while
-- properties_map_mv lacks ulice_id (so a re-run stays idempotent), which needs EXECUTE. It is
-- a create-or-replace of an existing function (its ACL survives) and the static revoke/grant
-- after it re-states the ACL; step 4 replaces it with 561's static body in the same file.

-- ---------------------------------------------------------------------------
-- 0. Preconditions. md5(prosrc) is the body as Postgres stores it. properties_map_visible may
--    also be the bridge (a re-run after step 3 committed and before step 4 did); the two
--    rebuild functions are 522's (steps 4-5 rely on their blue-green swap and their xact key).
--    Skipped in the CI schema-replay container (522 §5's probe: corpus < 100k properties).
-- ---------------------------------------------------------------------------
set statement_timeout = '900s';

do $pre$
declare
  populated bool;
  list_md5 text;
  map_md5  text;
begin
  select count(*) = 100000 into populated
    from (select 1 from public.properties limit 100000) probe;
  if not populated then
    raise notice '584: replay container (corpus < 100k properties), preconditions skipped';
    return;
  end if;
  select md5(prosrc) into list_md5 from pg_proc
   where oid = to_regprocedure('public.browse_list_visible()');
  select md5(prosrc) into map_md5 from pg_proc
   where oid = to_regprocedure('public.properties_map_visible()');
  if list_md5 is distinct from '47291211d34d2d14ca403dd3bbcdf176' then
    raise exception '584 refused: browse_list_visible() is not 561''s body (md5 %)', list_md5;
  end if;
  if map_md5 is null or map_md5 not in ('87ac5ed4f4c1eaaef58a06c9a4e42071', '051ba5e7f57b6d75d872ce7aae1c9123') then
    raise exception '584 refused: properties_map_visible() is neither 561''s body nor this '
                    'file''s bridge (md5 %)', map_md5;
  end if;
  if (select md5(prosrc) from pg_proc where oid = to_regprocedure('public.rebuild_browse_list()'))
     is distinct from '1386c849579ec1e490f0ad3447f9c895' then
    raise exception '584 refused: rebuild_browse_list() is not 522''s body';
  end if;
  if (select md5(prosrc) from pg_proc where oid = to_regprocedure('public.rebuild_properties_map_mv()'))
     is distinct from 'e18ea5161c8af9ceab5b07a62b6dc37d' then
    raise exception '584 refused: rebuild_properties_map_mv() is not 522''s body';
  end if;
end
$pre$;

-- ---------------------------------------------------------------------------
-- 1. Both rebuild locks first, queued; then fail fast.
-- ---------------------------------------------------------------------------
set statement_timeout = '1900s';
set lock_timeout = 0;

select pg_advisory_lock(hashtext('rebuild_browse_list'));
select pg_advisory_lock(hashtext('rebuild_properties_map_mv'));

set lock_timeout = '5s';
set statement_timeout = '900s';

-- ---------------------------------------------------------------------------
-- 2. TX A -- the three views no read model materializes.
-- ---------------------------------------------------------------------------
begin;

-- 567's body; ulice_id appended.
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
    st_y(ll.geom) as lat,
    st_x(ll.geom) as lng,
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
    mf.mf_reference_rent_czk,
    mf.mf_gross_yield_pct,
    ll.obec_name as obec,
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
    ll.obec_kod  as obec_id,
    ll.okres_kod as okres_id,
    ll.kraj_kod  as region_id,
    p.price_change_count,
    p.price_change_count_30d,
    p.price_change_count_90d,
    p.price_change_count_365d,
    p.total_price_change_pct,
    p.asset_id,
    mf.mf_reference_rent,
    p.published_at,
    p.repr_listing_ref_id as listing_id,
    l.source_id_native,
    measure_price_per_m2_basis(p.category_main, p.category_type) as price_per_m2_basis,
    location_display_label(ll.street_name, ll.house_number_cp, ll.house_number_co,
                           ll.obec_name, ll.cast_obce_name, ll.country_code,
                           ll.country_status) as display_label,
    ll.cast_obce_kod as cast_obce_id,
    ll.ulice_kod as ulice_id
from properties p
     left join listings l on l.id = p.repr_listing_ref_id
     left join listing_location ll on ll.listing_id = p.repr_listing_ref_id
     left join lateral public.mf_reference(
         p.category_main, p.category_type, p.disposition, p.area_m2, p.current_price_czk,
         p.condition, p.has_balcony, p.terrace, p.furnished, p.garage, p.has_lift,
         p.building_type, ll.obec_kod, ll.katastr_kod, ll.country_status) mf on true
where p.status = 'active'::text;

revoke all on properties_public from anon, authenticated;
grant select on properties_public to authenticated;

-- 508 §1f's body; ulice_id appended (create-or-replace keeps security_invoker as re-declared).
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
  -- ---- appended by migration 503 (W3 S1) ----
  p.display_label,
  -- ---- appended by migration 504 (W3 S3) ----
  p.cast_obce_id,
  -- ---- appended by migration 584 (street level, 2026-10-02) ----
  p.ulice_id
from property_pipeline_public pp
left join properties_public p on p.property_id = pp.property_id;

revoke all on pipeline_board_public from public, anon, authenticated;
grant select on pipeline_board_public to authenticated;

-- 567's body; ulice_id appended.
create or replace view listing_feed_public as
select
  l.id,
  l.source,
  l.source_id_native,
  l.sreality_id,
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
  rm.mf_gross_yield_pct,
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
  location_display_label(ll.street_name, ll.house_number_cp, ll.house_number_co,
                         ll.obec_name, ll.cast_obce_name, ll.country_code,
                         ll.country_status) as display_label,
  ll.cast_obce_kod as cast_obce_id,
  ll.uncertainty_radius_m,
  gr.rank::int as granularity_rank,
  public.plot_area_m2(l.category_main, l.area_m2::numeric,
                      l.estate_area::numeric) as plot_area_m2,
  ll.ulice_kod as ulice_id
from listings l
join properties p on p.id = l.property_id
left join listing_location ll on ll.listing_id = l.id
left join location_granularity_rank gr on gr.granularity = ll.granularity
left join lateral public.browse_list_mf(l.property_id) rm on true
where p.status = 'active'
  -- THE CONSUMER RULE == location_data.claims_common.SERVED_LOCATION_PREDICATE,
  --    rendered on the FEED's own listing. Pinned by tests/test_location_w5_serve_resolved.py.
  and EXISTS (SELECT 1 FROM listing_location sl WHERE sl.listing_id = l.id AND (sl.geom IS NOT NULL OR sl.country_status = 'foreign'));

revoke all on listing_feed_public from anon, authenticated;
grant select on listing_feed_public to authenticated;

commit;

-- ---------------------------------------------------------------------------
-- 3. TX B -- the table, then the projection, then the map's bridge.
-- ---------------------------------------------------------------------------
begin;

alter table public.browse_list add column if not exists ulice_id bigint;

-- 567's body; ulice_id appended.
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
    mf.mf_reference_rent_czk,
    mf.mf_gross_yield_pct,
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
    measure_price_per_m2_basis(p.category_main, p.category_type) as price_per_m2_basis,
    location_display_label(ll.street_name, ll.house_number_cp, ll.house_number_co,
                           ll.obec_name, ll.cast_obce_name, ll.country_code,
                           ll.country_status) as display_label,
    ll.cast_obce_kod as cast_obce_id,
    ll.uncertainty_radius_m,
    gr.rank::int as granularity_rank,
    public.plot_area_m2(p.category_main, p.area_m2::numeric,
                        p.estate_area::numeric) as plot_area_m2,
    ll.ulice_kod as ulice_id
from properties p
     left join listing_location ll on ll.listing_id = p.repr_listing_ref_id
     left join location_granularity_rank gr on gr.granularity = ll.granularity
     left join lateral public.mf_reference(
         p.category_main, p.category_type, p.disposition, p.area_m2, p.current_price_czk,
         p.condition, p.has_balcony, p.terrace, p.furnished, p.garage, p.has_lift,
         p.building_type, ll.obec_kod, ll.katastr_kod, ll.country_status) mf on true
where p.status = 'active'::text
  -- THE CONSUMER RULE (rule 25), read off the label join (522).
  and (ll.geom IS NOT NULL OR ll.country_status = 'foreign');

revoke all on browse_projection from anon, authenticated;
grant select on browse_projection to authenticated;

do $bridge$
begin
  if not exists (select 1 from pg_attribute
                  where attrelid = 'public.properties_map_mv'::regclass
                    and attname = 'ulice_id' and not attisdropped) then
    execute $fn$
create or replace function public.properties_map_visible()
returns setof public.browse_projection
language sql
stable
as $$
  select m.*, null::bigint as ulice_id from public.properties_map_mv m
  where not exists (
    select 1 from public.property_dismissals_public d where d.property_id = m.property_id)
$$
$fn$;
  end if;
end
$bridge$;

revoke execute on function public.properties_map_visible() from public, anon;
grant execute on function public.properties_map_visible() to authenticated, service_role;

commit;

-- ---------------------------------------------------------------------------
-- 4. The map, rebuilt (forced: this session holds the key), then 561's source, verbatim.
-- ---------------------------------------------------------------------------
set lock_timeout = 0;
set statement_timeout = '3600s';

begin;

select public.rebuild_properties_map_mv();

create or replace function public.properties_map_visible()
returns setof public.browse_projection
language sql
stable
as $$
  select m.* from public.properties_map_mv m
  where not exists (
    select 1 from public.property_dismissals_public d where d.property_id = m.property_id)
$$;

revoke execute on function public.properties_map_visible() from public, anon;
grant execute on function public.properties_map_visible() to authenticated, service_role;

commit;

-- ---------------------------------------------------------------------------
-- 5. The list, rebuilt (forced the same way) from the projection.
-- ---------------------------------------------------------------------------
select public.rebuild_browse_list();

-- ---------------------------------------------------------------------------
-- 6. Post-conditions. The SHAPE is asserted everywhere (it holds on the empty replay schema
--    too); the DATA only on production.
--
--    The fill is checked ROW BY ROW, not as count(browse_list) = count(browse_projection):
--    browse_list is a snapshot taken when step 5's CTAS began, and the location drain
--    re-resolves ~20k rows per 10 minutes (2026-10-02), so two counts taken minutes apart
--    differ by design and an equality would be a false red. Instead: every browse_list row
--    whose listing_location row was last written well before the rebuild began (resolved_at
--    is stamped by the one upsert on every write; 15 min of margin for a write transaction
--    that committed late) carries exactly that row's ulice_kod. A misaligned or unfilled
--    column fails this on ~150k rows; drift cannot fail it. "Began" is the rebuild's own
--    `last_succeeded_at`: it stamps now(), the START of its statement's transaction.
--
--    Every served code must name a ruian_streets row of SOME version (a code the register
--    never held = a misaligned column). One the register has since retired is not a failure
--    after all the DDL committed -- a registry refresh may retire a street -- only a NOTICE.
-- ---------------------------------------------------------------------------
do $post$
declare
  rel text;
  last_col text;
  populated bool;
  started timestamptz;
  n_checked bigint;
  n_wrong bigint;
  n_filled bigint;
  n_unknown bigint;
  n_retired bigint;
begin
  foreach rel in array array['browse_projection', 'browse_list', 'properties_map_mv',
                             'listing_feed_public', 'properties_public',
                             'pipeline_board_public'] loop
    select a.attname into last_col from pg_attribute a
     where a.attrelid = ('public.' || rel)::regclass and a.attnum > 0 and not a.attisdropped
     order by a.attnum desc limit 1;
    if last_col is distinct from 'ulice_id' then
      raise exception '584 did not land: the last column of % is %, not ulice_id', rel, last_col;
    end if;
  end loop;

  if (select md5(prosrc) from pg_proc
       where oid = to_regprocedure('public.properties_map_visible()'))
     is distinct from '87ac5ed4f4c1eaaef58a06c9a4e42071' then
    raise exception '584 did not land: properties_map_visible() is not 561''s body';
  end if;
  perform * from public.browse_list_visible() limit 1;
  perform * from public.properties_map_visible() limit 1;
  perform * from public.listing_feed_visible() limit 1;

  select count(*) = 100000 into populated
    from (select 1 from public.properties limit 100000) probe;
  if not populated then
    raise notice '584: replay container (corpus < 100k properties), data post-conditions skipped';
    return;
  end if;

  select last_succeeded_at into started from public.derived_artifacts where name = 'browse_list';
  if started is null or started < now() - interval '70 minutes' then
    raise exception '584 did not land: derived_artifacts carries no stamp of step 5''s '
                    'browse_list rebuild (last_succeeded_at %)', started;
  end if;

  select count(*), count(*) filter (where b.ulice_id is distinct from ll.ulice_kod)
    into n_checked, n_wrong
    from public.browse_list b
    join public.listing_location ll on ll.listing_id = b.listing_id
   where ll.resolved_at < started - interval '15 minutes';

  select count(*) filter (where b.ulice_id is not null),
         count(*) filter (where b.ulice_id is not null and not exists (
           select 1 from public.ruian_streets s where s.code = b.ulice_id)),
         count(*) filter (where b.ulice_id is not null and not exists (
           select 1 from public.ruian_streets s
            where s.code = b.ulice_id and s.valid_to is null))
    into n_filled, n_unknown, n_retired
    from public.browse_list b;

  if n_wrong > 0 or n_checked = 0 or n_filled = 0 then
    raise exception '584 did not land: % of % settled browse_list rows disagree with '
                    'listing_location.ulice_kod (% rows carry a street code)',
                    n_wrong, n_checked, n_filled;
  end if;
  if n_unknown > 0 then
    raise exception '584 did not land: % browse_list rows carry a ulice_id the register never '
                    'held (no ruian_streets row of any version)', n_unknown;
  end if;
  if n_retired > n_unknown then
    raise notice '584: % browse_list rows carry a street code the register has since retired '
                 '(ruian_streets.valid_to set)', n_retired - n_unknown;
  end if;

  raise notice '584: browse_list % rows with a street code; % settled rows checked, 0 disagree '
               '(rebuild began %)', n_filled, n_checked, started;
end
$post$;

-- ---------------------------------------------------------------------------
-- 7. Hand the locks back; PostgREST re-reads the widened relations.
-- ---------------------------------------------------------------------------
select pg_advisory_unlock(hashtext('rebuild_browse_list'));
select pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));

select pg_notify('pgrst', 'reload schema');

reset statement_timeout;
reset lock_timeout;
