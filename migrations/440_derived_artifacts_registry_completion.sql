-- 440: the freshness registry stops being a two-row special case.
--
-- Migration 437 shipped `derived_artifacts` (Corollary E: every precomputed artifact
-- declares its producer, its cadence and its staleness budget) with ONE real row, plus a
-- TEMPORARY union branch in `derived_artifacts_public` that adapted the two artifacts the
-- singleton `browse_read_model_state` has carried since migration 276. This migration
-- finishes the job: it registers the other thirteen artifacts, moves both blue-green
-- rebuild functions onto the registry, deletes the adapter, and drops the singleton.
--
-- AFTER THIS MIGRATION `public.derived_artifacts` holds 14 rows and
-- `public.derived_artifacts_public` publishes exactly those 14 -- one relation, one row per
-- artifact, no adapter and no duplicates.
--
-- WHAT IS DELIBERATELY *NOT* REGISTERED: `llm_cost_rollup_state`. It is a WATERMARK FOR an
-- artifact (`llm_cost_hour_rollup`), not an artifact -- it derives nothing and serves
-- nothing, it records how far that rollup is complete, which the rollup's own registry row
-- already publishes as `complete_through`. Registering it would publish a second, always
-- identical freshness signal for one artifact and invite a reader to treat a watermark as a
-- thing that can be stale on its own. `browse_map_cells` (migration 439, another lane) is
-- likewise absent: it is a partitioned TABLE, not a derived artifact this wave measured, and
-- its owning lane registers it if it is one.
--
-- ---------------------------------------------------------------------------------------
-- TWO BUDGETS ARE DELIBERATELY NOT THE CADENCE. Both reasons are measured, and both must
-- survive here, because a later session WILL "tidy" a budget to match its schedule.
--
-- 1. THE SIX HEALTH MATVIEWS GET 45 MINUTES, NOT 10. Re-measured on this instance
--    2026-08-26 over cron.job_run_details for jobid 1 (`refresh-health-dashboard`), gaps
--    between consecutive SUCCESSFUL refreshes:
--        window   n       avg     p95     max
--        90d      11,102  10.2    10.0    476.2   (minutes)
--        30d       3,486  11.1    15.8    476.2
--         7d         469  17.3    77.7    315.6
--    A 10-minute budget publishes RED on the normal case in every window, including the
--    healthy one, because a refresh that starts on time still ends after the next tick's
--    nominal boundary. 45 minutes clears the 30d p95 (15.8) with ~3x headroom.
--    HONEST CAVEAT, recorded rather than smoothed over: 45 minutes does NOT clear the
--    CURRENT 7-day p95 of 77.7 minutes. That window is a degraded regime, not the design
--    target -- the 90d p95 is 10.0 -- so the budget is set for the healthy cadence and the
--    panel showing this artifact red today is the registry reporting a real condition, not
--    a mis-set threshold. Re-derive from the 90d window, never the trailing week, if this
--    is ever revisited.
--    (The design note this wave was built from cited avg 19.2 / p95 78.2 / max 321.2. Those
--    reproduce as the 7-DAY window, not as the all-history figures they were presented as.
--    The conclusion -- 45, not 10 -- is unchanged; the provenance is corrected here.)
--
-- 2. `broker_region_type_stats` GETS 30 HOURS, NOT 24. Its producer is a DAILY sweep
--    (`35 4 * * *`) and the sweep itself is long: measured over all 72 completed full runs
--    in `broker_resolution_runs`, avg 42.5 min, p95 83.2 min, max 120.5 min. The matview is
--    refreshed in the sweep's TAIL, so consecutive stamps sit 24h apart PLUS the difference
--    in run length -- up to ~26h on the measured maximum. A 24-hour budget therefore alarms
--    on a perfectly healthy run. 30h is 24h + the measured worst-case run + margin.
--
-- 3. `rent_map_choropleth`'s host is spelled `api-request`, honestly, because that is what
--    it is: the matview is refreshed INSIDE a FastAPI request handler (`api/rent_map.py`,
--    `refresh materialized view rent_map_choropleth`), with `fetch_rent_map.yml` (`0 3 5 * *`)
--    as a monthly floor. Do not launder this into a cron string to make the column uniform --
--    an artifact whose freshness depends on someone loading a page is exactly the kind of
--    thing Corollary E exists to make visible.
-- ---------------------------------------------------------------------------------------
--
-- THE FUNCTION CUTOVER WAS NOT HAND-RETYPED. Migration 371 copied `rebuild_browse_list`
-- from an outdated body and silently reintroduced BOTH an `anon` grant and migration 277's
-- narrow 3-column indexes while claiming no behaviour change -- live-anon-readable for an
-- unknown period, fixed in 376. So both bodies below were captured from the LIVE catalog
-- (`pg_get_functiondef` / `prosrc` on erlvtprrmrylhznfyaih, 2026-08-26) and the ONLY edit is
-- the four-line stamp block. Verification hashes, so a reviewer can prove that:
--
--     md5(prosrc) BEFORE, live      rebuild_browse_list        e2b5e3220f8bb5d81ef3f09bcc379f7c  (2864 chars)
--                                   rebuild_properties_map_mv  9997436666360ab044d9a56fabcd14b4  (2143 chars)
--     md5(pg_get_functiondef) live  rebuild_browse_list        b33cdcb3dfce2630117b394f377d07bd
--                                   rebuild_properties_map_mv  e33eaa2f74ebe7ef3a1aa28e03519af4
--
-- DRIFT FOUND AND PRESERVED, NOT "FIXED". The live bodies differ from the newest on-disk
-- definition (migration 376) by exactly ONE character in ONE line, in each function: the
-- `raise exception` message reads `(see migration 374)` live and `(see migration 376)` on
-- disk. 376 is the correct reference (374 is `374_city_quality_browse_list.sql`, which
-- contains no self-check); 376's own header records that it was applied under an earlier
-- number before a concurrent branch forced a renumber, which is how the message and the
-- filename came apart. The LIVE text is carried forward verbatim so the md5s above actually
-- verify. Correcting the message is a one-word forward migration for whoever wants it -- it
-- is deliberately NOT bundled here, because "I also changed one other thing" is precisely
-- how 371 happened.
--
-- ci-allow-dynamic: rebuild_browse_list / rebuild_properties_map_mv -- both blue-green their
-- object through `EXECUTE`'d DDL every tick and have since migrations 276/277, so the offline
-- scanner in test_migration_rls_grants.py cannot inspect it (dollar-quoted bodies are opaque
-- there by design). That blind spot is covered by the DEDICATED gate
-- tests/test_browse_grant_drift.py, which does reach inside the EXECUTE strings for exactly
-- these two relations and fails on any `anon` re-grant, plus the runtime self-check inside
-- both bodies. Same annotation, same reasoning as migration 376.
--
-- Everything else in both bodies is untouched and load-bearing: the `authenticated`-only
-- grant (never `anon`), the post-grant `has_table_privilege` self-check that RAISEs and
-- rolls back the whole tick, migration 283's NINE-column covering indexes on browse_list,
-- the `analyze ..._next` BEFORE the rename, and the `pg_notify('pgrst','reload schema')`
-- after it. Rails: tests/test_browse_grant_drift.py and
-- tests/test_browse_read_path_guardrail.py.

-- =======================================================================================
-- PART 1 -- registry rows + both producers + the adapter's removal. ONE TRANSACTION.
-- =======================================================================================
--
-- ORDERING IS THE WHOLE RISK OF THIS WAVE, and it is not the order the wave was drafted in.
-- The draft said: cut the functions over, wait a tick, then seed. That cannot work. The
-- stamp is `update derived_artifacts ... where name = 'browse_list'`, and an UPDATE that
-- matches no row is a SILENT no-op -- so between a cutover and a later seed both rebuilds
-- would stamp nothing at all, and the "wait one tick, then check last_succeeded_at is
-- fresh" verification could never pass. The rows must exist BEFORE the producers reference
-- them. Hence: seed and cut over together.
--
-- Seeding and deleting the union branch must ALSO be one transaction -- between them,
-- `browse_list` and `properties_map_mv` would each appear TWICE in
-- `derived_artifacts_public` (once from the new base row, once from the adapter still
-- reading the singleton) and the Health panel would double-render them.
--
-- Both requirements are satisfied by doing all three here. What is deliberately left to
-- PART 2 is the DROP of `browse_read_model_state`: dropping it before the functions stop
-- writing to it would make the very next tick RAISE inside `exception when others then
-- pg_advisory_unlock(...); raise;`, roll back the entire rebuild, and stop republishing
-- `browse_list` -- a Browse outage inside 15 minutes.

begin;

-- The two rebuild functions are replaced below. A tick that is MID-EXECUTION holds a lock
-- on its own pg_proc entry, so without this the CREATE OR REPLACE queues behind a rebuild
-- (measured live at up to 600s, the job's statement_timeout) while holding every lock this
-- transaction has already taken. Fail fast and retry in a quiet window instead.
set local lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 1. The eleven artifacts with no stamp to carry over
-- ---------------------------------------------------------------------------
--
-- Seeded with NULL freshness columns on purpose: nothing has ever written a stamp for these,
-- and inventing one would publish a freshness that was never observed. Each becomes live the
-- first time its producer is taught to stamp -- which is a per-producer follow-up, not this
-- wave. Until then the registry publishes what is true: declared cadence and budget, unknown
-- last success.
--
-- Artifact names appear ONLY in INSERTs in this migration, never in a CREATE VIEW statement.
-- Seven of these names are in tests/test_migration_rls_grants.py's `_ADMIN_ONLY_RELATIONS`
-- (health_summary_mv, portal_health_mv, scraper_health_checks_mv, category_trends_mv,
-- snapshot_churn_24h_mv, image_storage_overview_mv, images_failure_overview_mv), and its
-- `test_new_admin_objects_embed_the_gate` substring-matches those names against the text of
-- every CREATE VIEW/FUNCTION statement. INSERT statements are not scanned.
insert into public.derived_artifacts
  (name, producer, host, cadence, staleness_budget, is_serving)
values
  -- The six matviews of the health dashboard, all refreshed together by ONE pg_cron
  -- function (verified: refresh_health_matviews' body refreshes exactly these six,
  -- concurrently, in this order). See budget note 1 in the header for why 45 and not 10.
  ('health_summary_mv',         'refresh_health_matviews',      'pg_cron', '*/10 * * * *', interval '45 minutes', true),
  ('portal_health_mv',          'refresh_health_matviews',      'pg_cron', '*/10 * * * *', interval '45 minutes', true),
  ('scraper_health_checks_mv',  'refresh_health_matviews',      'pg_cron', '*/10 * * * *', interval '45 minutes', true),
  ('category_trends_mv',        'refresh_health_matviews',      'pg_cron', '*/10 * * * *', interval '45 minutes', true),
  ('snapshot_churn_24h_mv',     'refresh_health_matviews',      'pg_cron', '*/10 * * * *', interval '45 minutes', true),
  ('health_mv_refresh_stamp',   'refresh_health_matviews',      'pg_cron', '*/10 * * * *', interval '45 minutes', true),

  -- Refreshed in the TAIL of the daily FULL broker sweep only -- `_refresh_matview` is
  -- called from `_run_full` and from nowhere else, so the `*/10` incremental
  -- (broker_resolution.yml) never touches it. Budget note 2 explains 30h, not 24h.
  ('broker_region_type_stats',  'scripts/resolve_brokers.py',   'github-actions', '35 4 * * *',   interval '30 hours', true),

  -- Weekly, in the price-stats scrape (scrape_price_stats.yml -> scraper/price_stats_db.py,
  -- `refresh materialized view [concurrently] price_stat_choropleth`). 9 days = one 7-day
  -- cadence plus a two-day grace, so a single skipped Monday does not alarm but two do.
  ('price_stat_choropleth',     'scraper/price_stats_db.py',    'github-actions', '0 4 * * 1',    interval '9 days',  true),

  -- Corollary E's own exhibit: refreshed inside a FastAPI request handler, with a monthly
  -- workflow as the floor. Budget note 3 -- do NOT rewrite `host` as a cron string.
  ('rent_map_choropleth',       'api/rent_map.py + fetch_rent_map.yml', 'api-request', 'on-demand + 0 3 5 * *', interval '40 days', true),

  -- Both refreshed by one script at the tail of the 2-hourly image workflow
  -- (images.yml -> scripts/refresh_image_stats.py, whose _MVS tuple is exactly this pair).
  -- 6h = three cadences, so a single skipped run is absorbed and a stuck one is not.
  ('image_storage_overview_mv', 'scripts/refresh_image_stats.py', 'github-actions', '0 */2 * * *', interval '6 hours', true),
  ('images_failure_overview_mv','scripts/refresh_image_stats.py', 'github-actions', '0 */2 * * *', interval '6 hours', true)
on conflict (name) do nothing;

-- ---------------------------------------------------------------------------
-- 2. The two artifacts that DO have a stamp to carry over
-- ---------------------------------------------------------------------------
--
-- Seeded FROM the singleton rather than as NULL, so the switch-over is continuous: at COMMIT
-- the Health panel starts reading these two from the base table with byte-identical values
-- to what the adapter was publishing a moment earlier. Without this they would read
-- "never succeeded" until the next tick -- and `browse_list`'s next tick is not guaranteed
-- (see the note at the end of PART 1).
--
-- `now()` (transaction time) is what the old stamp used and what the new one uses, so
-- `complete_through` = `last_succeeded_at` for both: these are full snapshot rebuilds, and
-- the stamp is written at COMPLETION, so the artifact is complete through the moment it
-- succeeded. Same equality migration 437's adapter asserted.
insert into public.derived_artifacts
  (name, producer, host, cadence, staleness_budget,
   complete_through, last_succeeded_at, last_duration_ms, last_rows, is_serving)
select 'browse_list', 'rebuild_browse_list', 'pg_cron', '*/15 * * * *', interval '45 minutes',
       b.list_rebuilt_at, b.list_rebuilt_at, b.list_duration_ms, b.list_rows::bigint, true
  from public.browse_read_model_state b where b.id = 1
on conflict (name) do nothing;

insert into public.derived_artifacts
  (name, producer, host, cadence, staleness_budget,
   complete_through, last_succeeded_at, last_duration_ms, last_rows, is_serving)
select 'properties_map_mv', 'rebuild_properties_map_mv', 'pg_cron', '7,37 * * * *', interval '90 minutes',
       b.map_rebuilt_at, b.map_rebuilt_at, b.map_duration_ms, b.map_rows::bigint, true
  from public.browse_read_model_state b where b.id = 1
on conflict (name) do nothing;

-- ---------------------------------------------------------------------------
-- 3. rebuild_browse_list -- live body, stamp block only
-- ---------------------------------------------------------------------------
--
-- THE STAMP IS A PLAIN UPDATE AND STAYS ONE. If the registry row is missing the stamp is
-- silently lost, and that is the correct trade: the alternative (`get diagnostics` +
-- `raise`) would put the whole Browse read model behind the existence of a metadata row, so
-- deleting one registry row would take Browse down within 15 minutes. An INSERT ... ON
-- CONFLICT DO UPDATE would be self-healing but would have to spell producer/host/cadence/
-- budget inside the function, giving the registry two sources of truth. The row's existence
-- is guaranteed offline instead, by tests/test_derived_artifacts_registry.py.
create or replace function rebuild_browse_list()
returns void
language plpgsql
security definer
set search_path = public
as $fn$
declare
  t0 timestamptz := clock_timestamp();
  n  bigint;
begin
  if not pg_try_advisory_lock(hashtext('rebuild_browse_list')) then
    raise notice 'rebuild_browse_list: previous run still active, skipping tick';
    return;
  end if;
  begin
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
  exception when others then
    perform pg_advisory_unlock(hashtext('rebuild_browse_list'));
    raise;
  end;
  perform pg_advisory_unlock(hashtext('rebuild_browse_list'));
end
$fn$;

-- ---------------------------------------------------------------------------
-- 4. rebuild_properties_map_mv -- live body, stamp block only
-- ---------------------------------------------------------------------------
create or replace function rebuild_properties_map_mv()
returns void
language plpgsql
security definer
set search_path = public
as $fn$
declare
  t0 timestamptz := clock_timestamp();
  n  bigint;
begin
  if not pg_try_advisory_lock(hashtext('rebuild_properties_map_mv')) then
    raise notice 'rebuild_properties_map_mv: previous run still active, skipping tick';
    return;
  end if;
  begin
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
        include (sreality_id, price_czk, disposition, subtype, area_m2, district,
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
  exception when others then
    perform pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));
    raise;
  end;
  perform pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));
end
$fn$;

-- ---------------------------------------------------------------------------
-- 5. The adapter comes out
-- ---------------------------------------------------------------------------
--
-- Same ten columns, same order, same types, same grant, same posture as migration 437 --
-- only the UNION ALL branch is gone. The column list IS the contract (the SPA reads this
-- view with an explicit projection and `tests/test_derived_artifacts_registry.py::
-- test_the_public_view_exposes_exactly_the_ten_declared_columns` pins it), so this is
-- CREATE OR REPLACE and not a drop/recreate: Postgres will reject the statement outright if
-- a column name, order or type moved, which is the behaviour we want.
--
-- Still no is_platform_admin() gate, matching migration 318's classification of the view
-- this replaces: aggregate-only operational metadata (rebuild timing), no row-level content.
-- Postgres-owned and deliberately NOT security_invoker -- owner rights are what read through
-- the RLS wall on the base table. anon is deliberately absent; the SPA runs as authenticated.
--
-- NO ARTIFACT NAME MAY APPEAR ANYWHERE IN THIS STATEMENT. See the note on the seed above.
create or replace view public.derived_artifacts_public as
  select a.name, a.producer, a.host, a.cadence, a.staleness_budget,
         a.complete_through, a.last_succeeded_at,
         a.last_duration_ms, a.last_rows, a.is_serving
    from public.derived_artifacts a;

grant select on public.derived_artifacts_public to authenticated;

commit;

-- ---------------------------------------------------------------------------------------
-- BETWEEN PART 1 AND PART 2, VERIFY -- this is not optional bookkeeping.
--
--   select prosrc ~ 'browse_read_model_state' from pg_proc
--    where proname in ('rebuild_browse_list','rebuild_properties_map_mv');   -- => f, f
--   select count(*) from public.derived_artifacts;                           -- => 14
--   select count(*) from public.derived_artifacts_public;                    -- => 14
--
-- and then let each producer run once and confirm its row advanced. Both did, through the
-- new stamp, on 2026-08-26:
--     browse_list        07:00:01  408,436 ms  583,354 rows
--     properties_map_mv  07:07:00  214,139 ms  557,422 rows
--
-- NOTE FOR WHOEVER READS THE SURROUNDING HISTORY, because it looks alarming and is not this
-- migration's doing: `rebuild_browse_list` failed SEVEN consecutive ticks earlier that
-- morning -- 05:15, 05:30, 05:45, 06:00, 06:15, 06:30 and 06:45 each ended `failed` after
-- exactly 600s, its statement_timeout -- leaving the read model unrebuilt from 04:45 to
-- 07:00. It recovered on its own at 07:00 in 6m48s. The rebuild therefore runs at roughly
-- 70 percent of its own timeout on a good tick, which is why a bad one tips the whole run
-- over; that cost is a separate, filed problem and NOT something this wave changes. What
-- this wave changes is that the condition is now VISIBLE: `browse_list` declares a 45-minute
-- budget, so a stall like that morning's publishes as stale instead of being invisible.
-- ---------------------------------------------------------------------------------------

-- =======================================================================================
-- PART 2 -- the singleton goes. DESTRUCTIVE; runs only after PART 1 is verified.
-- =======================================================================================
--
-- Backed up first, in full -- the table is one row of six numbers and every one of them has
-- just been copied into `derived_artifacts` by PART 1. Contents at drop time, recorded here
-- so the drop is reversible from this file alone:
--
--   id | list_rebuilt_at                  | list_duration_ms | list_rows
--    1 | 2026-08-26T04:45:00.075765+00:00 |           465814 |    583169
--      | map_rebuilt_at                   | map_duration_ms  | map_rows
--      | 2026-08-26T04:37:00.053983+00:00 |           368714 |    557254
--
-- Ordering rule, restated because getting it wrong is a Browse outage rather than a failed
-- migration: this DROP is safe ONLY because PART 1 already committed and neither rebuild
-- function references `browse_read_model_state` any more. Verify `prosrc ~
-- 'browse_read_model_state'` is false for BOTH functions before running this. Dropping first
-- makes the next tick raise inside its own exception handler, roll back the entire rebuild,
-- and stop republishing browse_list.
--
-- The view is dropped before the table it reads. `browse_read_model_state_public` is the
-- last consumer; the SPA reader that used it (`fetchBrowseReadModelState`) is deleted in the
-- same change, and the Health panel's registry section (migration 437) already covers both
-- artifacts -- the two stale-banners it fed are redundant now, not lost.

begin;

set local lock_timeout = '5s';

drop view  if exists public.browse_read_model_state_public;
drop table if exists public.browse_read_model_state;

commit;

-- ---------------------------------------------------------------------------------------
-- AFTER PART 2:
--   select to_regclass('public.browse_read_model_state'),
--          to_regclass('public.browse_read_model_state_public');             -- => NULL, NULL
-- ---------------------------------------------------------------------------------------
