-- 493_location_compare_cohort.sql
--
-- Precompute the /location-compare cohort ONCE, instead of once per statement.
--
-- THE FAILURE (production, 2026-09-10, operator screenshot): "QueryCanceled:
-- canceling statement due to statement timeout" on the page's FILTERS - OKRESY
-- section, against a 120 s per-statement budget.
--
-- THE CAUSE. Every one of the page's 6-9 statements rebuilt the SAME cohort CTE
-- from scratch: browse_list b (700k rows, UNLOGGED, wide) LEFT JOIN
-- property_location_current p (637k) LEFT JOIN listing_location_current w (756k,
-- very wide - geom, ltree, labels), filtered by
-- `b.region_id = ANY(kraje) OR p.kraj_kod = ANY(kraje)`. That OR spans two
-- tables, so no index can drive it: the planner seq-scans both, hash-joins, and
-- applies the predicate afterwards - ~1,130 MB of heap read PER STATEMENT. On top
-- of that the okres aggregate joined `mv m ON m.old_okres_id = o.kod OR
-- m.new_okres_kod = o.kod`, an OR-join the planner can only serve as a nested
-- loop that re-scans the cohort once per district: 810k of the plan's 1.02M cost.
-- pg_stat_statements (cold): the four scope() statements averaged 22 s + 9 s +
-- 5 s + 33 s, max 97.5 s - so a cold page load was 70-160 s of cohort rebuilds
-- and went over budget the moment the instance was busy (it is: the Railway
-- resolve lane rewrites both projections continuously, ~5 rows/s).
--
-- THE FIX. Build the whole-corpus cohort once every 30 minutes into an UNLOGGED
-- plain table and let every statement read THAT. The page's meaning does not
-- change: the select list below is toolkit/location_compare.py's `_COHORT_CTE`
-- verbatim (same joins, same `b.is_active`, same winner join, same Praha
-- sentinel nulling), minus the kraje predicate - which moves into the module as
-- a WHERE on this table - plus one new column, `scope_kraj_kod`.
--
-- WHY scope_kraj_kod IS ITS OWN COLUMN. The live cohort predicate is
-- `p.kraj_kod` (the PROPERTY rollup) while every counter on the page compares
-- `w.kraj_kod` (the WINNER row). They agree on today's data (0 of 637,381 rows
-- differ), but by coincidence, not by construction - a rollup that ever lags its
-- winner would silently change WHICH properties the page reviews. Both are
-- stored: `scope_kraj_kod` = p.kraj_kod scopes, `new_kraj_kod` = w.kraj_kod
-- counts.
--
-- WHY THIS IS NOT A MATERIALIZED VIEW. rebuild_browse_list() (migration 277,
-- pg_cron every 15 min) does `drop table if exists browse_list` on every tick. A
-- matview over browse_list would take a pg_depend entry on it, that DROP would
-- fail, and Browse's read model would freeze platform-wide. A plain table has no
-- dependency edge, so the two rebuilds are independent. Do not "fix" this into a
-- matview.
--
-- STALENESS is now visible and bounded: 30 min (this job) + 10 min
-- (CACHE_TTL_S in the module) = 40 min worst case. The page's header stamp reads
-- `location_compare_cohort_state.refreshed_at`, so it tells the truth about the
-- numbers below it instead of about the request clock. An operator correction
-- landing in the projections does not move the compare numbers until the next
-- tick, by design - this is a review surface, not a live query.
--
-- ci-allow-dynamic: refresh_location_compare_cohort builds its blue-green
--   replacement through EXECUTE, the same idiom as rebuild_browse_list()
--   (migration 277).
-- ci-allow-ungated: refresh_location_compare_cohort is a write-only maintenance
--   function returning void, not a read path over admin-only rows.

-------------------------------------------------------------------
-- 1. The shell (empty; the first refresh fills it).
-------------------------------------------------------------------
-- LIMIT 0 keeps the migration instant and makes the projection the single
-- source of every column type, exactly as migration 276 did for browse_list.
-- The ::text casts are load-bearing: _SCOPE_BY_METHOD_SQL does
-- `coalesce(admin_assignment_method, 'no_row')`, and an enum cannot coalesce
-- with a non-member literal.

create unlogged table if not exists location_compare_cohort as
  select b.property_id,
         b.listing_id,
         b.source,
         b.lat AS old_lat,
         b.lng AS old_lng,
         b.region_id AS old_region_id,
         -- LEGACY_PRAHA_OKRES_SENTINEL (#1391): legacy stamps every Prague
         -- listing okres_id = 9999 and no such unit exists; the new side stores
         -- NULL, so the sides are only comparable once this is nulled.
         nullif(b.okres_id, 9999) AS old_okres_id,
         b.obec_id AS old_obec_id,
         coalesce(b.place_search_text, concat_ws(', ', b.obec, b.okres)) AS old_label,
         (w.listing_id IS NOT NULL) AS has_row,
         p.kraj_kod AS scope_kraj_kod,
         w.kraj_kod AS new_kraj_kod,
         w.okres_kod AS new_okres_kod,
         w.obec_kod AS new_obec_kod,
         w.cast_obce_kod AS new_cast_obce_kod,
         w.ulice_kod AS new_ulice_kod,
         w.display_label AS new_label,
         w.geom AS new_geom,
         w.granularity::text AS granularity,
         w.match_confidence::text AS match_confidence,
         w.admin_assignment_method::text AS admin_assignment_method,
         w.position_source::text AS position_source,
         w.uncertainty_radius_m::double precision AS uncertainty_radius_m,
         w.distance_to_nearest_boundary_m::double precision
           AS distance_to_nearest_boundary_m,
         w.renderable_as_point,
         w.render_as,
         w.pin_collision_class,
         w.location_disputed,
         gr.rank AS granularity_rank,
         p.member_spread_m::double precision AS member_spread_m,
         p.disagreement_flags,
         p.member_count
  from browse_list b
  left join property_location_current p on p.property_id = b.property_id
  left join listing_location_current  w on w.listing_id = p.winner_listing_id
  left join location_granularity_rank gr on gr.granularity = w.granularity
  where b.is_active
  limit 0;

-------------------------------------------------------------------
-- 2. Indexes (five). The refresh recreates these on _next each cycle -
--    keep the two lists in lockstep.
-------------------------------------------------------------------
-- The two kraj indexes ARE the read-time point of this change: the predicate
-- that used to span two tables (so nothing could drive it) is now a BitmapOr of
-- two partial index scans on one relation. At the shipped 2-kraj scope (~44% of
-- rows) the planner will still choose a 120 MB seq scan - that is this design's
-- floor and is ~10x inside the budget.
--
-- Deliberately ABSENT: old_okres_id / new_okres_kod / old_obec_id /
-- new_obec_kod. Every statement that touches those is a grouped aggregate over
-- the whole scoped cohort, or a CASE on a bound %(level)s parameter - an index
-- can drive neither, and four more index builds would ride on every refresh.
create unique index if not exists location_compare_cohort_pk
  on location_compare_cohort (property_id);
create index if not exists location_compare_cohort_old_region_idx
  on location_compare_cohort (old_region_id) where old_region_id is not null;
create index if not exists location_compare_cohort_scope_kraj_idx
  on location_compare_cohort (scope_kraj_kod) where scope_kraj_kod is not null;
create index if not exists location_compare_cohort_old_pos_idx
  on location_compare_cohort (old_lat, old_lng)
  where old_lat is not null and old_lng is not null;
create index if not exists location_compare_cohort_new_geom_idx
  on location_compare_cohort using gist (new_geom) where new_geom is not null;

-------------------------------------------------------------------
-- 3. The refresh stamp. LOGGED, on purpose.
-------------------------------------------------------------------
-- The cohort is UNLOGGED (no WAL for 48 rebuilds/day; crash recovery truncates
-- it and the next tick refills). The STAMP must survive that crash, or the page
-- would print a confident timestamp over an empty table - which is why the
-- module proves readiness with an EXISTS on the cohort itself and uses this row
-- only for "when". `row_count`, not `rows`: keyword hygiene.
create table location_compare_cohort_state (
  id           smallint primary key default 1 check (id = 1),
  refreshed_at timestamptz,
  duration_ms  integer,
  row_count    bigint
);
insert into location_compare_cohort_state (id) values (1) on conflict (id) do nothing;

-------------------------------------------------------------------
-- 4. Posture: backend-only, like every other location_* relation
--    (migrations 380 / 404). No grants to a browser role at all, and no
--    `_public` view - the compare page is admin-gated at the API.
-------------------------------------------------------------------
alter table location_compare_cohort enable row level security;
alter table location_compare_cohort_state enable row level security;
revoke all on location_compare_cohort       from anon, authenticated;
revoke all on location_compare_cohort_state from anon, authenticated;
-- The function's own REVOKE lives at the END of section 5, after the CREATE:
-- `revoke ... on function` has no IF EXISTS form and raises 42883 against a name
-- pg_proc does not hold yet, which would abort this whole migration - and a
-- `create or replace` that CREATES re-applies the default EXECUTE TO PUBLIC, so a
-- revoke placed before it would be undone by the very statement it guards.

-------------------------------------------------------------------
-- 5. The refresh: blue-green, a near-copy of rebuild_browse_list().
-------------------------------------------------------------------
-- NO `set statement_timeout` in the option clauses: migration 371 established
-- that a function's own proconfig can never raise the budget for its own
-- execution, and tests/test_cron_statement_timeout_guard.py lints for exactly
-- that mistake. The cron command in §6 arms the real budget.
--
-- Build off to the side, ANALYZE, then swap - so the ACCESS EXCLUSIVE window is
-- the last few statements, not the whole ~2-minute build. On a lock_timeout at
-- the swap the function raises, the OLD snapshot keeps serving, and the next
-- tick's `drop ... _next` reclaims the orphan.
--
-- pg_try_advisory_XACT_lock, and therefore NO `exception when others` unlock
-- handler: plpgsql's OTHERS matches every error class EXCEPT query_canceled and
-- assert_failure, so a statement_timeout (57014) or an operator pressing cancel -
-- the two failures this guard exists for - would skip the handler and strand a
-- SESSION lock. That is harmless from pg_cron (the backend exits) but not from the
-- manual first-population run in section 6, whose pooled session lives on: every
-- later tick would then log 'previous run still active' and return success while
-- the snapshot silently stopped refreshing. A transaction lock releases on commit
-- OR abort, in every one of those paths, with no handler to get right.
create or replace function refresh_location_compare_cohort()
returns void
language plpgsql
security definer
set search_path = public
as $fn$
declare
  t0 timestamptz := clock_timestamp();
  n  bigint;
begin
  if not pg_try_advisory_xact_lock(hashtext('refresh_location_compare_cohort')) then
    raise notice 'refresh_location_compare_cohort: previous run still active, skipping tick';
    return;
  end if;
  execute 'drop table if exists location_compare_cohort_next';
  -- LIKE makes §1's shell the single source of the shape: a column or type
  -- drift fails the INSERT loudly instead of publishing a differently-shaped
  -- table under the live name.
  execute 'create unlogged table location_compare_cohort_next (like location_compare_cohort)';
  execute $q$
    insert into location_compare_cohort_next
    select b.property_id,
           b.listing_id,
           b.source,
           b.lat,
           b.lng,
           b.region_id,
           nullif(b.okres_id, 9999),
           b.obec_id,
           coalesce(b.place_search_text, concat_ws(', ', b.obec, b.okres)),
           (w.listing_id is not null),
           p.kraj_kod,
           w.kraj_kod,
           w.okres_kod,
           w.obec_kod,
           w.cast_obce_kod,
           w.ulice_kod,
           w.display_label,
           w.geom,
           w.granularity::text,
           w.match_confidence::text,
           w.admin_assignment_method::text,
           w.position_source::text,
           w.uncertainty_radius_m::double precision,
           w.distance_to_nearest_boundary_m::double precision,
           w.renderable_as_point,
           w.render_as,
           w.pin_collision_class,
           w.location_disputed,
           gr.rank,
           p.member_spread_m::double precision,
           p.disagreement_flags,
           p.member_count
    from browse_list b
    left join property_location_current p on p.property_id = b.property_id
    left join listing_location_current  w on w.listing_id = p.winner_listing_id
    left join location_granularity_rank gr on gr.granularity = w.granularity
    where b.is_active
  $q$;
  -- Keep in lockstep with §2.
  execute 'create unique index location_compare_cohort_next_pk on location_compare_cohort_next (property_id)';
  execute 'create index location_compare_cohort_next_old_region_idx on location_compare_cohort_next (old_region_id) where old_region_id is not null';
  execute 'create index location_compare_cohort_next_scope_kraj_idx on location_compare_cohort_next (scope_kraj_kod) where scope_kraj_kod is not null';
  execute 'create index location_compare_cohort_next_old_pos_idx on location_compare_cohort_next (old_lat, old_lng) where old_lat is not null and old_lng is not null';
  execute 'create index location_compare_cohort_next_new_geom_idx on location_compare_cohort_next using gist (new_geom) where new_geom is not null';
  -- The replacement carries the same posture as §4, so the swap can never
  -- publish an RLS-off relation under the live name.
  execute 'alter table location_compare_cohort_next enable row level security';
  execute 'analyze location_compare_cohort_next';
  execute 'select count(*) from location_compare_cohort_next' into n;

  -- The swap: short ACCESS EXCLUSIVE window, and never queued behind a live
  -- page statement.
  set local lock_timeout = '5s';
  execute 'drop table if exists location_compare_cohort';
  execute 'alter table location_compare_cohort_next rename to location_compare_cohort';
  execute 'alter index location_compare_cohort_next_pk rename to location_compare_cohort_pk';
  execute 'alter index location_compare_cohort_next_old_region_idx rename to location_compare_cohort_old_region_idx';
  execute 'alter index location_compare_cohort_next_scope_kraj_idx rename to location_compare_cohort_scope_kraj_idx';
  execute 'alter index location_compare_cohort_next_old_pos_idx rename to location_compare_cohort_old_pos_idx';
  execute 'alter index location_compare_cohort_next_new_geom_idx rename to location_compare_cohort_new_geom_idx';
  execute 'revoke all on location_compare_cohort from anon, authenticated';

  -- Upsert, not UPDATE: a lost seed row would otherwise match nothing and
  -- strand the page's stamp at "generating..." forever.
  insert into location_compare_cohort_state (id, refreshed_at, duration_ms, row_count)
  values (1, now(),
          (extract(epoch from clock_timestamp() - t0) * 1000)::integer, n)
  on conflict (id) do update
    set refreshed_at = excluded.refreshed_at,
        duration_ms  = excluded.duration_ms,
        row_count    = excluded.row_count;

  -- The platform catalog (migration 437, Corollary E). Plain UPDATE, matching
  -- rebuild_browse_list(): the row's existence is guaranteed offline by
  -- tests/test_derived_artifacts_registry.py, and a stamp that raised here would
  -- put the snapshot behind the existence of a metadata row. Written in the SAME
  -- transaction as the state row above, so the two can never disagree - the state
  -- row is what the PAGE reads, this is what the Health dashboard reads.
  update derived_artifacts
     set last_succeeded_at = now(),
         complete_through  = now(),
         last_duration_ms  = (extract(epoch from clock_timestamp() - t0) * 1000)::integer,
         last_rows         = n
   where name = 'location_compare_cohort';
end
$fn$;

-- AFTER the CREATE, never before it: `revoke ... on function` has no IF EXISTS
-- form, and a `create or replace` that actually creates re-applies the default
-- EXECUTE TO PUBLIC - which anon and authenticated inherit - so this is the only
-- position where it both parses and holds.
revoke all on function refresh_location_compare_cohort() from public, anon, authenticated;

-- Corollary E: a precomputed artifact declares its producer, cadence and staleness
-- budget. 70 min = two 30 min ticks plus slack, so one skipped run is not an alarm
-- and two consecutive ones are.
insert into public.derived_artifacts
  (name, producer, host, cadence, staleness_budget, is_serving)
values ('location_compare_cohort', 'refresh_location_compare_cohort', 'pg_cron',
        '11,41 * * * *', interval '70 minutes', true)
on conflict (name) do nothing;

-------------------------------------------------------------------
-- 6. The schedule: every 30 min at :11 / :41.
-------------------------------------------------------------------
-- The six live jobs occupy */10 (health), */15 (browse-list), 7,37
-- (browse-map), 4,19,34,49 (llm-cost), :10 and 30 */6. :11/:41 collides with none.
-- Cost: ~2 x 1.13 GB/h read, ~0 WAL (UNLOGGED). `set statement_timeout` as its OWN
-- statement in the cron command is the only place it takes effect (migration 371).
--
-- WHY THE BUDGET IS 240 s AND NOT THE 600 s THIS JOB COULD AFFORD. The refresh reads
-- browse_list, so it holds ACCESS SHARE on it for its WHOLE transaction (index
-- builds and swap included, not just the INSERT). rebuild_browse_list() republishes
-- browse_list with `drop table if exists browse_list` and arms NO lock_timeout of
-- its own (migration 277), so an overlap does not merely queue that job: Postgres
-- parks every SUBSEQUENT browse_list reader behind its pending ACCESS EXCLUSIVE, and
-- Browse - the operator-facing product, read by the SPA on a 3 s anon budget - stalls
-- platform-wide until this refresh commits.
-- Live cron.job_run_details, 24 h (2026-09-10): browse-list-rebuild ran 96 times,
-- mean 325 s, max 603 s - i.e. it is capped by its own 600 s budget, so a tick that
-- starts at :00 is always finished by :10, and :11 is genuinely clear. The exposure
-- is the OTHER direction: 600 s here would let a :11 run reach :21, straight through
-- the :15 tick's own swap. 240 s cannot reach :15 at all, which is what makes the
-- overlap impossible rather than merely unlikely. If the build ever outgrows 240 s,
-- move the slot or the cadence - never widen the budget back into the rebuild window.
--
-- NO `select refresh_location_compare_cohort();` here: at the 120 s database
-- default this migration's own apply would be killed mid-build. The first
-- population is a rollout step, run from the SQL editor with a raised budget;
-- until then the page honestly reads "generating...". A cancelled or timed-out
-- manual run strands nothing: the lock is transaction-scoped (section 5).
do $cron$
begin
  create extension if not exists pg_cron;
  perform cron.schedule(
    'location-compare-cohort-refresh',
    '11,41 * * * *',
    $$set statement_timeout='240s'; select public.refresh_location_compare_cohort();$$
  );
exception when others then
  raise notice 'pg_cron unavailable; location-compare cohort not scheduled (%). '
               'Call refresh_location_compare_cohort() from another scheduler.', sqlerrm;
end
$cron$;

-------------------------------------------------------------------
-- 7. Comments.
-------------------------------------------------------------------
comment on table location_compare_cohort is
  'Precomputed whole-corpus cohort for the admin /location-compare page '
  '(migration 493): every active browse_list property joined to '
  'property_location_current and its winner listing_location_current row, the '
  'exact projection toolkit/location_compare.py used to rebuild per statement. '
  'Derived and rebuildable in ~2 min; written ONLY by '
  'refresh_location_compare_cohort() from pg_cron (job '
  '''location-compare-cohort-refresh'', 11,41 * * * *). UNLOGGED by design: no '
  'WAL for 48 rebuilds/day, and crash recovery truncating it is safe - the '
  'module probes EXISTS and shows "generating..." until the next tick. Kill '
  'switch: cron.alter_job(..., active := false) - the page keeps serving the '
  'last snapshot with a visibly ageing stamp. NOTE: granularity_rank is '
  'denormalized from the static location_granularity_rank lookup, so an edit to '
  'that table is invisible here for up to 30 min.';

comment on table location_compare_cohort_state is
  'One row (id = 1): when refresh_location_compare_cohort() last published a '
  'snapshot, how long it took, and how many rows it wrote. LOGGED on purpose - '
  'the cohort it describes is UNLOGGED, so this stamp has to outlive a crash '
  'that truncates it. The compare page reads refreshed_at as its "generated" '
  'header; readiness is proven separately by an EXISTS on the cohort itself.';
