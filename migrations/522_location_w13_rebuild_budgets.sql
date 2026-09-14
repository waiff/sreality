-- 522_location_w13_rebuild_budgets.sql
--
-- W13. THE READ-MODEL REBUILD BUDGET MUST SCALE WITH THE CORPUS, AND A CANCELLED
-- REBUILD MUST NOT LEAVE A LOCK.
--
-- THE OUTAGE (measured 2026-09-14). `browse_list` froze at 315,827 active rows from
-- 14:26Z while the location store underneath RECOVERED to 712,932 rows with a town.
-- Browse served a two-and-a-half-hour-old market. Two pg_cron jobs, two distinct
-- failures, ONE cause:
--
--   * `browse-list-rebuild` (*/15) timed out 11 times in 6 h. Its rebuild had grown
--     173 s -> 257 s -> 405 s -> past the 600 s budget as the corpus came back, always
--     inside `create unlogged table browse_list_next as select * from browse_projection`.
--     The budget was a constant; the corpus is not. A `*/15` job that burns 10 of every
--     15 minutes and then throws the work away is worse than no job at all.
--   * `browse-map-rebuild` (7,37) stopped firing ENTIRELY after 13:07Z -- no
--     `cron.job_run_details` row, and no "cron job 7 starting" line in the server log,
--     so the scheduler never launched it. Verified it was NOT wedged: no advisory lock
--     held for `rebuild_properties_map_mv` (`pg_locks`), no orphaned
--     `properties_map_mv_next`, no stale `starting`/`running` row, no zombie backend.
--     It was STARVED. The failing */15 rebuild kept the box in `DataFileRead` for 10
--     minutes out of every 15, the pg_cron scheduler loop lagged past the minute
--     boundary (jobs due at :10 observed starting at :11:33), and pg_cron does not
--     catch up a MISSED slot. `*/10` and `*/15` jobs get another slot moments later
--     and survive that; a job whose whole schedule is two single minutes an hour --
--     :07 and :37, both of which fall inside a */15 rebuild window -- does not. The
--     map's silence was a SYMPTOM of the list's timeout loop, not a second fault.
--
-- WHAT THIS FILE CHANGES, in the order it has to happen.
--   1. The COST, so the rebuild fits its budget instead of the budget chasing it.
--      W5 (migration 514) appended the consumer rule to `browse_projection` as an
--      EXISTS against a SECOND alias of `listing_location`. But the projection ALREADY
--      LEFT JOINs `listing_location ll` on the SAME key for the label and the chip
--      codes, and `listing_location_pkey` is UNIQUE on `listing_id` -- so that join
--      yields at most one row and the rule can be read straight off it. The EXISTS was
--      a second full pass over an 821k-row table for an answer already in hand. 514's
--      cost note ("one more hash of a relation the projection already scans") was right
--      about the shape and wrong about the price: the planner drove the whole join from
--      `sl` and reached `properties` by index scan. Same rows -- guaranteed by the
--      unique key, and probed on production (40,000 properties, 0 disagreements).
--      Plan cost on the isolated join skeleton: 283,226 -> 229,197, one 821k-row pass
--      instead of two.
--   2. The LOCK, so a cancelled rebuild cannot poison the next tick. Both functions
--      took a SESSION advisory lock and released it in an `exception when others`
--      handler -- and PL/pgSQL's OTHERS does not match QUERY_CANCELED. Every one of the
--      11 statement-timeout cancellations skipped that handler entirely. Only
--      pg_cron's disconnect-per-run released the lock; the same function called from a
--      pooled session would have wedged every later run into "skipping tick" forever.
--      `pg_try_advisory_xact_lock` is released by the transaction -- on commit, on
--      rollback, on cancel -- so the handler is not needed and cannot be got wrong.
--   3. The BUDGETS, scaled to the corpus with headroom: list 600 s -> 1800 s, map
--      600 s -> 1500 s. The list's interval stays */15: if a rebuild ever outlives its
--      own interval the next tick finds the lock held, says so with a NOTICE and
--      returns cleanly, which is the correct behaviour and now the only behaviour.
--      THE TIMEOUT IS ARMED IN THE CRON COMMAND, never in the function's proconfig --
--      migration 371's lesson, held by tests/test_cron_statement_timeout_guard.py:
--      Postgres arms statement_timeout once, when the top-level statement begins.
--   4. Both rebuilds, forced, so Browse and the map are current when this file returns.
--
-- IDEMPOTENT + STATEMENT AUTOCOMMIT, like 514: no begin/commit, because
-- `apply_migration.yml` retries the whole file on a lock timeout and a partial apply
-- must be resumable. Every statement here is `create or replace`, an upserting
-- `cron.schedule`, or a rebuild that is blue-green and self-skipping.
--
-- ADDITIVE. No column is added, dropped, retyped or repositioned; `sync_browse_list`
-- inserts into `browse_list` POSITIONALLY, so the select list below is 514's verbatim.

-- ci-allow-dynamic: rebuild_browse_list / rebuild_properties_map_mv blue-green their
-- relations through `EXECUTE`'d DDL on every tick and have since migrations 276/277,
-- so the offline scanner in tests/test_migration_rls_grants.py cannot inspect them
-- (dollar-quoted bodies are opaque there by design). This file changes only the lock
-- each one takes; every EXECUTE string is carried over unchanged. The blind spot stays
-- covered by the DEDICATED gate tests/test_browse_grant_drift.py, which reaches inside
-- the EXECUTE strings for exactly these two relations and fails on any `anon` re-grant,
-- plus the runtime self-check inside each body that refuses to publish a rebuild if
-- `anon` somehow holds SELECT (migration 376).

-- ---------------------------------------------------------------------------
-- 0. Take both rebuild locks FIRST, in this psql session (`apply_migration.yml`
--    runs the file with a single `psql -f`, so a session lock spans the file).
--    While they are held every pg_cron tick self-skips in milliseconds instead of
--    starting a 10-minute rebuild, which is what makes the DDL below uncontended.
--    Waiting is deliberate and bounded: `lock_timeout = 0` so the acquire QUEUES
--    behind an in-flight rebuild rather than aborting, under a 900 s statement
--    budget that comfortably outlasts the old 600 s one. The new functions take
--    the SAME advisory keys transaction-scoped, and session and transaction
--    advisory locks share one lock space, so this holds against both generations.
-- ---------------------------------------------------------------------------
set statement_timeout = '900s';
set lock_timeout = 0;

select pg_advisory_lock(hashtext('rebuild_browse_list'));
select pg_advisory_lock(hashtext('rebuild_properties_map_mv'));

-- Now that no rebuild can start, the ACCESS EXCLUSIVE the view swap needs is
-- uncontended. Fail fast anyway (the hot-table DDL rule): the only remaining
-- holder would be a `sync_browse_list` read, and the workflow retries the file.
set lock_timeout = '30s';

-- ---------------------------------------------------------------------------
-- 1. browse_projection -- migration 514's body, VERBATIM, with the consumer rule
--    re-spelled onto the label join it was already duplicating. Column list and
--    column ORDER are byte-identical to 514.
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
-- 2. The two rebuild functions. ONE mechanical change each: the session advisory
--    lock becomes a TRANSACTION advisory lock, and the `exception when others`
--    handler that existed only to release it goes with it.
--
--    Why that handler was never the safety net it looked like: PL/pgSQL's OTHERS
--    deliberately does not match QUERY_CANCELED (or ASSERT_FAILURE). A
--    statement_timeout cancel -- all 11 of them on 2026-09-14 -- unwinds straight
--    past it, so `pg_advisory_unlock` never ran. The lock survived only because
--    pg_cron opens a fresh connection per run and a session lock dies with the
--    session; called from the tenant pool or any pooled session, the FIRST cancel
--    would have left the key held and every later run would have returned
--    "skipping tick" forever, silently, with `job_run_details` reporting success.
--    `pg_try_advisory_xact_lock` cannot fail that way: the lock is owned by the
--    transaction and Postgres releases it on commit, rollback AND cancel.
--
--    Everything else is byte-for-byte 512's body: blue-green build, `analyze`
--    BEFORE the swap, the anon self-check from migration 376, the
--    `derived_artifacts` stamp and the PostgREST schema reload.
-- ---------------------------------------------------------------------------

create or replace function public.rebuild_browse_list()
returns void
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
  t0 timestamptz := clock_timestamp();
  n  bigint;
begin
  -- Transaction-scoped: released by commit, rollback OR cancel (W13).
  if not pg_try_advisory_xact_lock(hashtext('rebuild_browse_list')) then
    raise notice 'rebuild_browse_list: previous run still active, skipping tick';
    return;
  end if;

  execute 'drop table if exists browse_list_next';
  execute $q$
    create unlogged table browse_list_next as
    select * from browse_projection
    order by category_main, category_type, first_seen_at
  $q$;
  execute 'create unique index browse_list_next_pk on browse_list_next (property_id)';
  execute 'create index browse_list_next_cat_first_seen_idx on browse_list_next (category_main, category_type, first_seen_at desc, property_id desc)';
  execute 'create index browse_list_next_obec_price_idx on browse_list_next (obec_id, category_type, price_czk, property_id, category_main, subtype, disposition, area_m2, is_active) where obec_id is not null';
  execute 'create index browse_list_next_okres_price_idx on browse_list_next (okres_id, category_type, price_czk, property_id, category_main, subtype, disposition, area_m2, is_active) where okres_id is not null';
  execute 'create index browse_list_next_region_price_idx on browse_list_next (region_id, category_type, price_czk, property_id, category_main, subtype, disposition, area_m2, is_active) where region_id is not null';
  execute 'analyze browse_list_next';
  execute 'select count(*) from browse_list_next' into n;

  execute 'drop table if exists browse_list';
  execute 'alter table browse_list_next rename to browse_list';
  execute 'alter index browse_list_next_pk rename to browse_list_pk';
  execute 'alter index browse_list_next_cat_first_seen_idx rename to browse_list_cat_first_seen_idx';
  execute 'alter index browse_list_next_obec_price_idx rename to browse_list_obec_price_idx';
  execute 'alter index browse_list_next_okres_price_idx rename to browse_list_okres_price_idx';
  execute 'alter index browse_list_next_region_price_idx rename to browse_list_region_price_idx';
  execute 'grant select on browse_list to authenticated';
  execute 'revoke insert, update, delete, truncate on browse_list from anon, authenticated';

  if has_table_privilege('anon', 'browse_list', 'SELECT') then
    raise exception 'rebuild_browse_list: anon must never hold SELECT on browse_list -- refusing to publish this rebuild (see migration 374)';
  end if;

  update derived_artifacts
     set last_succeeded_at = now(),
         complete_through  = now(),
         last_duration_ms  = (extract(epoch from clock_timestamp() - t0) * 1000)::integer,
         last_rows         = n
   where name = 'browse_list';
  perform pg_notify('pgrst', 'reload schema');
end
$function$;

create or replace function public.rebuild_properties_map_mv()
returns void
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
  t0 timestamptz := clock_timestamp();
  n  bigint;
begin
  -- Transaction-scoped: released by commit, rollback OR cancel (W13).
  if not pg_try_advisory_xact_lock(hashtext('rebuild_properties_map_mv')) then
    raise notice 'rebuild_properties_map_mv: previous run still active, skipping tick';
    return;
  end if;

  execute 'drop materialized view if exists properties_map_mv_next';
  execute $q$
    create materialized view properties_map_mv_next as
    select * from browse_projection
    where lat is not null and lng is not null
    order by category_main, category_type, lat, lng
  $q$;
  execute 'create unique index properties_map_mv_next_pk on properties_map_mv_next (property_id)';
  execute $q$
    create index properties_map_mv_next_cover on properties_map_mv_next
      (category_main, category_type, lat, lng)
      include (sreality_id, price_czk, disposition, subtype, area_m2,
               last_seen_at, first_seen_at, is_active)
  $q$;
  execute 'analyze properties_map_mv_next';
  execute 'select count(*) from properties_map_mv_next' into n;

  execute 'drop materialized view if exists properties_map_mv';
  execute 'alter materialized view properties_map_mv_next rename to properties_map_mv';
  execute 'alter index properties_map_mv_next_pk rename to properties_map_mv_pk';
  execute 'alter index properties_map_mv_next_cover rename to properties_map_mv_cover';
  execute 'grant select on properties_map_mv to authenticated';

  if has_table_privilege('anon', 'properties_map_mv', 'SELECT') then
    raise exception 'rebuild_properties_map_mv: anon must never hold SELECT on properties_map_mv -- refusing to publish this rebuild (see migration 374)';
  end if;

  update derived_artifacts
     set last_succeeded_at = now(),
         complete_through  = now(),
         last_duration_ms  = (extract(epoch from clock_timestamp() - t0) * 1000)::integer,
         last_rows         = n
   where name = 'properties_map_mv';
  perform pg_notify('pgrst', 'reload schema');
end
$function$;

-- ---------------------------------------------------------------------------
-- 3. The budgets. Guarded exactly like migrations 136 / 274 / 510 / 514: the CI
--    schema-replay container has no pg_cron and this file must still apply there.
--    `cron.schedule` upserts by job name, so these REPLACE the live commands and
--    the names stay the ones every runbook and dashboard already knows.
--
--    1800 s for the list: 4.4x the last good rebuild (405 s) and 3x the healthy
--    one (~257 s), which is headroom for a corpus that grew 2.3x in a day, not a
--    number chosen to make today's failure pass. 1500 s for the map, which builds
--    the same projection filtered to rows with a pin.
--
--    The list's INTERVAL stays */15. A rebuild that outlives it is not an error:
--    the next tick finds the advisory lock held, emits its NOTICE and returns --
--    the skip path, which section 2 just made cancel-proof. (The NOTICE lands in
--    the server log, not in `cron.job_run_details.return_message` -- pg_cron
--    records the command tag, and for a two-statement command the tag it reports
--    is the leading `SET`. Reading a `SET` there means "still in flight or
--    skipped", never "rebuilt"; that is how the 16:30Z tick was misread as a
--    success while its backend was still running.)
-- ---------------------------------------------------------------------------
do $cron$
begin
  create extension if not exists pg_cron;
  perform cron.schedule(
    'browse-list-rebuild',
    '*/15 * * * *',
    $$set statement_timeout='1800s'; select public.rebuild_browse_list();$$
  );
  perform cron.schedule(
    'browse-map-rebuild',
    '7,37 * * * *',
    $$set statement_timeout='1500s'; select public.rebuild_properties_map_mv();$$
  );
exception when others then
  raise notice 'pg_cron unavailable; browse read-model rebuild budgets not rescheduled (%). Reschedule both jobs on whatever scheduler runs them.', sqlerrm;
end
$cron$;

-- ---------------------------------------------------------------------------
-- 4. Hand the locks back and force both rebuilds, list first (the map reads the
--    same projection and Browse is the surface that is frozen).
--
--    `lock_timeout = 0` is REQUIRED here, not laziness: the blue-green swap takes
--    ACCESS EXCLUSIVE on `browse_list` while PostgREST readers hold ACCESS SHARE,
--    and a lock_timeout would throw away twenty minutes of completed work at the
--    final rename. pg_cron runs these with no lock_timeout; so does this.
--
--    Each rebuild QUEUES for its own advisory key before calling the function, so a
--    tick already in flight is waited out rather than raced. Advisory locks are
--    re-entrant within a session, so the `pg_try_advisory_xact_lock` inside the
--    function then succeeds against this very transaction and the rebuild is
--    GUARANTEED to run -- no retry loop, no sleeping, and nothing to stall the CI
--    replay. Two separate DO blocks, so they autocommit independently: if the map
--    were to fail it must not roll back the list rebuild Browse is waiting on.
-- ---------------------------------------------------------------------------
select pg_advisory_unlock(hashtext('rebuild_browse_list'));
select pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));

set statement_timeout = '3600s';
set lock_timeout = 0;

do $force_list$
begin
  perform pg_advisory_xact_lock(hashtext('rebuild_browse_list'));
  perform public.rebuild_browse_list();
end
$force_list$;

do $force_map$
begin
  perform pg_advisory_xact_lock(hashtext('rebuild_properties_map_mv'));
  perform public.rebuild_properties_map_mv();
end
$force_map$;

-- ---------------------------------------------------------------------------
-- 5. Post-conditions. Asserted on production, SKIPPED with a notice in the CI
--    schema-replay container, where every table is empty and a row-count floor
--    could only ever be a false red (tests/test_migration_catalog_guards.py: a
--    guard that cannot fire is worse than no guard). The corpus probe is bounded
--    so it costs nothing on either side.
-- ---------------------------------------------------------------------------
do $assert$
declare
  populated bool;
  n_list    bigint;
  n_map     bigint;
  map_at    timestamptz;
begin
  select count(*) = 100000 into populated
    from (select 1 from public.properties limit 100000) probe;

  if not populated then
    raise notice '522: replay container (corpus < 100k properties), post-conditions skipped';
    return;
  end if;

  execute 'select count(*) from public.browse_list where is_active' into n_list;
  if n_list <= 330000 then
    raise exception '522: browse_list has only % active rows after the forced rebuild -- expected > 330000 (it froze at 315,827 during the outage)', n_list;
  end if;

  execute 'select count(*) from public.properties_map_mv' into n_map;
  select last_succeeded_at into map_at
    from public.derived_artifacts where name = 'properties_map_mv';
  if n_map <= 380000 and (map_at is null or map_at < now() - interval '10 minutes') then
    raise exception '522: properties_map_mv is neither larger than 380000 rows (%) nor refreshed in the last 10 minutes (%)', n_map, map_at;
  end if;

  raise notice '522: browse_list % active rows, properties_map_mv % rows refreshed %', n_list, n_map, map_at;
end
$assert$;

reset statement_timeout;
reset lock_timeout;
