-- 437: the /costs page stops re-aggregating llm_calls on every load, and every
-- derived artifact gains a place to declare its own freshness.
--
-- WHY (measured warm, 2026-08-25, against erlvtprrmrylhznfyaih):
--   The daily view's 35-day query reads 58,878 heap rows to emit 90 -- 10,439 shared
--   blocks. The hourly view's real steady-state (measured on a July-shape 49h window,
--   because the OpenAI credit outage since 08-15 has driven current traffic to ~59
--   calls/day and makes every "today" reading understate July by ~100x) is ~1,250
--   blocks for 159 rows. Output cardinality is nearly free -- the same window renders
--   90 daily rows for 10,439 blocks and 1,152 hourly rows for 11,885, i.e. 12.8x the
--   output for 1.14x the cost -- so the cost is entirely in the scan, and the scan is
--   what this migration removes.
--
-- THE EARN-TEST. A pure rewrite was tried first: bounding the SPA's one-sided range
-- flips the plan from an ordered Index Scan feeding GroupAggregate to a Bitmap Heap
-- Scan and costs 11,885 -> 4,033 blocks for free. That is a 2.95x win and it is still
-- ~200x above the rows-on-screen floor, so it does not earn its way out of precompute.
-- It is also SUPERSEDED, not additive: after this migration no /costs query scans
-- llm_calls unbounded, so the client-side upper bound would buy nothing on top of it.
-- Do not do both, and do not cite 2.95x as a benefit of this wave.
--
-- THE SHAPE: store ONE grain (the UTC hour), derive the day at read time, and serve
-- reads as [closed hours from the rollup] UNION ALL [the open edge, live from
-- llm_calls]. Prague-day re-aggregation from UTC hour buckets is EXACT, proven over
-- all 293,561 rows: re-grouping hour buckets to a Prague day produced 331 groups,
-- byte-identical to grouping llm_calls directly by Prague day, with zero rows in
-- either symmetric difference including the cost column. Prague's offsets over
-- 2024-01-01..2027-01-01 are exactly {+1h, +2h} with no fractional-hour sample, so a
-- Prague day boundary always lands on a UTC hour boundary and never splits a bucket.
--
-- ROLLUP SIZE: 3,199 hour-groups over all history. Sized deliberately against the
-- SURVIVING workload and not against the July peak: 71.4 percent of llm_calls history
-- was produced by two permanently retired workloads (the three compare_* dedup feeders
-- stopped dead 2026-08-06 at the teardown, 151,249 calls; score_listing_condition
-- stopped 06-18, 58,436 calls). The surviving workload's worst-ever 35-day window is
-- 307 hour-groups, not the 2,565 the mixed history shows. Nothing here is keyed to
-- that peak; a later retention or partitioning decision must not use it as input.

begin;

-- ---------------------------------------------------------------------------
-- 1. The store
-- ---------------------------------------------------------------------------

create table if not exists public.llm_cost_hour_rollup (
  bucket_hour        timestamptz not null,
  called_for         text        not null,
  provider           text        not null,
  model              text        not null,
  calls              integer     not null,
  error_calls        integer     not null,
  -- UNROUNDED, deliberately. round() belongs at the OUTER projection of each view and
  -- NOWHERE else. Storing the rounded hourly value and summing 24 of them is
  -- sum-of-rounds, not round-of-sum: measured over all history that corrupts 74 of 331
  -- daily groups (max error 0.0003 USD). llm_calls.cost_usd is numeric(10,6), so the
  -- per-hour sums here are exact and the only way to lose fidelity is to round early.
  cost_usd           numeric     not null,
  input_tokens       bigint      not null,
  output_tokens      bigint      not null,
  cache_read_tokens  bigint      not null,
  cache_write_tokens bigint      not null,
  primary key (bucket_hour, called_for, provider, model)
) with (
  -- Every refresh tick rewrites the trailing ~3h of buckets via ON CONFLICT DO UPDATE.
  -- The SET list touches only measure columns; bucket_hour -- the sole input to both the
  -- PK and the Prague-day expression index -- is never modified, so those updates are
  -- genuinely HOT-eligible and fillfactor is a real win rather than wasted space.
  -- 96 ticks/day rewriting 1-30 rows each is roughly one full turnover per day on a
  -- ~3,200-row table, which is why the autovacuum thresholds are tightened too.
  fillfactor = 70,
  autovacuum_vacuum_scale_factor  = 0.05,
  autovacuum_vacuum_threshold     = 50,
  autovacuum_analyze_scale_factor = 0.05
);

-- Not decoration, and not load-bearing either -- stated honestly. The SPA's day filter
-- arrives as a qual on a derived grouping column, which Postgres pushes through the
-- outer GROUP BY and distributes into the UNION ALL branches, where this index answers
-- it. Without it the daily view seq-scans the rollup, which is ~3,200 rows / ~55 pages
-- today and grows ~1,700 rows per 35 days at the arithmetic ceiling -- so even the
-- un-indexed fallback beats today's 10,439 blocks by ~7x five years from now.
create index if not exists llm_cost_hour_rollup_prague_day_idx
  on public.llm_cost_hour_rollup (((bucket_hour at time zone 'Europe/Prague')::date));

-- The REVOKE is belt-and-braces over the RLS wall, and it is needed: measured,
-- pg_default_acl for objects postgres creates in public is
-- {postgres=arwdDxtm, authenticated=r, service_role=arwdDxtm} -- a new table is handed
-- authenticated:SELECT automatically. anon gets nothing. Same posture
-- browse_read_model_state has carried since migration 276.
alter table public.llm_cost_hour_rollup enable row level security;
revoke all on public.llm_cost_hour_rollup from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- 2. The watermark
-- ---------------------------------------------------------------------------

create table if not exists public.llm_cost_rollup_state (
  id boolean primary key default true check (id),
  complete_through timestamptz not null,
  -- THE UNION'S CORRECTNESS PROOF, AS A CONSTRAINT. The split
  --   [rollup: bucket_hour < W] UNION ALL [live: called_at >= W]
  -- is an exact partition only if W lands on a UTC hour boundary, because
  -- bucket_hour = floor_hour(called_at): for hour-aligned W, called_at >= W is
  -- equivalent to bucket_hour >= W. An off-boundary W silently double-counts or drops
  -- the straddling hour, forever, with nothing failing anywhere. date_trunc(text,
  -- timestamp) is IMMUTABLE (verified), so this is enforceable as a CHECK rather than
  -- left to implementation habit -- and it holds for '-infinity'.
  constraint llm_cost_rollup_state_hour_aligned
    check (date_trunc('hour', complete_through at time zone 'UTC')
             = complete_through at time zone 'UTC')
);

-- Seed BELOW everything. A fresh install therefore serves an empty rollup branch and a
-- whole-table live branch -- i.e. exactly today's numbers at roughly today's cost --
-- until the backfill at the bottom of this migration advances it. There is no window
-- in which the page is wrong or blank.
insert into public.llm_cost_rollup_state (id, complete_through)
values (true, '-infinity') on conflict (id) do nothing;

alter table public.llm_cost_rollup_state enable row level security;
revoke all on public.llm_cost_rollup_state from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- 3. The registry (F5-minimal)
-- ---------------------------------------------------------------------------
--
-- Corollary E: every precomputed artifact declares its producer, its cadence and its
-- staleness budget, and its freshness is observable. This is the minimal shape that
-- does that -- one row per artifact, not the singleton-with-a-column-pair-per-artifact
-- that browse_read_model_state has been since 276.
--
-- WHAT IS DELIBERATELY ABSENT: last_error / has_error, and last_started_at.
-- A plpgsql `exception when others then update ...; raise;` handler CANNOT persist that
-- write -- the re-raise unwinds the subtransaction the handler ran in. Verified live on
-- this instance: a function that writes in its handler and re-raises leaves the table
-- EMPTY. So a last_error column could only ever be NULL and a published has_error could
-- only ever read false -- including during a total outage. That is the same defect class
-- as migration 432's guard that could not fire, and a health signal that cannot fire is
-- worse than no signal, because the panel renders it green. The durable failure record
-- is cron.job_run_details; the observable freshness signal is last_succeeded_at falling
-- behind staleness_budget, which stays correct precisely BECAUSE it rolls back with a
-- failed run.
create table if not exists public.derived_artifacts (
  name              text primary key,
  producer          text        not null,
  host              text        not null,
  cadence           text        not null,
  staleness_budget  interval    not null,
  complete_through  timestamptz,
  last_succeeded_at timestamptz,
  last_duration_ms  integer,
  last_rows         bigint,
  is_serving        boolean     not null default true
);

alter table public.derived_artifacts enable row level security;
revoke all on public.derived_artifacts from public, anon, authenticated;

insert into public.derived_artifacts
  (name, producer, host, cadence, staleness_budget, is_serving)
values ('llm_cost_hour_rollup', 'refresh_llm_cost_rollups', 'pg_cron',
        '4,19,34,49 * * * *', interval '1 hour', true)
on conflict (name) do nothing;

-- No is_platform_admin() gate, matching migration 318's own classification of
-- browse_read_model_state_public: aggregate-only, non-sensitive operational metadata
-- (rebuild timing), no row-level content. Postgres-owned and deliberately NOT
-- security_invoker -- owner rights are exactly what lets it read through the RLS wall
-- the base table just raised. anon is deliberately absent; the SPA runs as authenticated.
--
-- NOTHING FROM _ADMIN_ONLY_RELATIONS MAY EVER APPEAR IN THIS STATEMENT. CI's
-- test_new_admin_objects_embed_the_gate substring-matches the whole CREATE VIEW text
-- against that frozenset, and seven of the twelve matviews this registry will eventually
-- cover are on it by name. Artifact names belong in seed INSERTs (which the scanner does
-- not inspect), never in the view body.
create or replace view public.derived_artifacts_public as
  select a.name, a.producer, a.host, a.cadence, a.staleness_budget,
         a.complete_through, a.last_succeeded_at,
         a.last_duration_ms, a.last_rows, a.is_serving
    from public.derived_artifacts a
  union all
  -- TEMPORARY. W7 deletes this branch together with browse_read_model_state and its
  -- _public view; until then it adapts the two rows the singleton state table already
  -- carries WITHOUT editing either rebuild function. Their stamp is written at
  -- COMPLETION, so rebuilt_at is both "last succeeded" and "complete through".
  select v.name, v.producer, 'pg_cron', v.cadence, v.staleness_budget,
         v.rebuilt_at, v.rebuilt_at,
         v.duration_ms, v.n_rows, true
    from public.browse_read_model_state b
    cross join lateral (values
      ('browse_list', 'rebuild_browse_list', '*/15 * * * *', interval '45 minutes',
        b.list_rebuilt_at, b.list_duration_ms, b.list_rows),
      ('properties_map_mv', 'rebuild_properties_map_mv', '7,37 * * * *', interval '90 minutes',
        b.map_rebuilt_at, b.map_duration_ms, b.map_rows)
    ) as v(name, producer, cadence, staleness_budget, rebuilt_at, duration_ms, n_rows);

grant select on public.derived_artifacts_public to authenticated;

-- ---------------------------------------------------------------------------
-- 4. The union source (internal, ungated, un-grantable)
-- ---------------------------------------------------------------------------
--
-- ci-allow-ungated: llm_cost_hour_union -- every privilege is revoked below and no grant
-- is ever made, so no browser role can reach it; both PUBLIC wrappers over it carry
-- is_platform_admin() in their outermost scope. The gate MUST stay outside the set
-- operation: is_platform_admin() is STABLE and zero-arg, so in the outer query it renders
-- as a One-Time Filter on a Result node above the Append and short-circuits the whole
-- plan. Pushed into the branches it would be evaluated per branch; pushed inside the
-- union it would stop hoisting entirely.
--
-- This is a VIEW rather than inlined text on purpose: it gives the daily view a
-- definition containing only 'Europe/Prague' and the hourly view a definition containing
-- no zone at all, which is what makes the three-zone-literal rail a clean set-equality
-- assertion instead of a fragile substring hunt (pg_get_viewdef does not expand a
-- referenced view).
create or replace view public.llm_cost_hour_union as
  select r.bucket_hour as bucket, r.called_for, r.provider, r.model,
         r.calls, r.error_calls, r.cost_usd,
         r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens
    from public.llm_cost_hour_rollup r
    -- An uncorrelated scalar subquery with a fallback, NEVER a join to the state table.
    -- With an inner join, a missing singleton row makes BOTH views return zero rows and
    -- /costs silently goes blank. With this, a missing row degrades the views to exactly
    -- today's behaviour (empty rollup branch, live branch = whole table). Uncorrelated,
    -- so it renders as an InitPlan and is evaluated once.
   where r.bucket_hour < coalesce(
           (select s.complete_through from public.llm_cost_rollup_state s where s.id),
           '-infinity'::timestamptz)
  union all
  select (date_trunc('hour', l.called_at at time zone 'UTC') at time zone 'UTC'),
         l.called_for, l.provider, l.model,
         count(*)::integer,
         count(*) filter (where l.error is not null)::integer,
         sum(l.cost_usd),
         sum(l.input_tokens)::bigint,
         sum(l.output_tokens)::bigint,
         sum(l.cache_read_tokens)::bigint,
         sum(l.cache_write_tokens)::bigint
    from public.llm_calls l
    -- Predicates on RAW called_at, which is what keeps it cheap: measured warm on a
    -- July-shape 3-hour window, 179 shared blocks / 618 rows, HashAggregate over an
    -- Index Scan on llm_calls_called_at_idx.
   where l.called_at >= coalesce(
           (select s.complete_through from public.llm_cost_rollup_state s where s.id),
           '-infinity'::timestamptz)
   group by 1, 2, 3, 4;

revoke all on public.llm_cost_hour_union from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- 5. The two public views -- names, columns, types and gate unchanged
-- ---------------------------------------------------------------------------
--
-- Every cast below is load-bearing. sum(integer) returns bigint and sum(bigint) returns
-- numeric, so re-aggregating for the daily view without them would silently change six
-- column types on views the SPA reads with .select('*'). CREATE OR REPLACE VIEW rejects
-- a type change outright, so a dropped cast fails this migration loudly rather than
-- shipping a contract break.

create or replace view public.llm_cost_hourly_public as
select bucket, called_for, provider, model, calls, error_calls, cost_usd,
       input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
from (
  select u.bucket, u.called_for, u.provider, u.model,
         u.calls, u.error_calls,
         round(u.cost_usd, 4) as cost_usd,          -- the ONLY round on this path
         u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens
  from public.llm_cost_hour_union u
) __admin_gate
where is_platform_admin();

create or replace view public.llm_cost_daily_public as
select day, called_for, provider, model, calls, error_calls, cost_usd,
       input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
from (
  select (u.bucket at time zone 'Europe/Prague')::date as day,
         u.called_for, u.provider, u.model,
         sum(u.calls)::integer             as calls,
         sum(u.error_calls)::integer       as error_calls,
         round(sum(u.cost_usd), 4)         as cost_usd,   -- round AFTER the sum, once
         sum(u.input_tokens)::bigint       as input_tokens,
         sum(u.output_tokens)::bigint      as output_tokens,
         sum(u.cache_read_tokens)::bigint  as cache_read_tokens,
         sum(u.cache_write_tokens)::bigint as cache_write_tokens
  from public.llm_cost_hour_union u
  group by 1, u.called_for, u.provider, u.model
) __admin_gate
where is_platform_admin();

revoke all on public.llm_cost_daily_public  from anon;
revoke all on public.llm_cost_hourly_public from anon;
grant select on public.llm_cost_daily_public  to authenticated;
grant select on public.llm_cost_hourly_public to authenticated;

-- ---------------------------------------------------------------------------
-- 6. The producer
-- ---------------------------------------------------------------------------
--
-- ci-allow-ungated: refresh_llm_cost_rollups -- EXECUTE is revoked from every browser
-- role below; the only callers are pg_cron and the operator.
--
-- NO `exception when others` HANDLER, deliberately. See the derived_artifacts comment
-- above: a handler's write cannot survive its own re-raise (verified live), so an error
-- handler here could only produce a stamp that is always rolled back. Letting it raise
-- puts the failure in cron.job_run_details, where it is durable, and leaves
-- last_succeeded_at un-advanced, which is the signal the registry actually publishes.
create or replace function public.refresh_llm_cost_rollups(p_from timestamptz default null)
returns integer
language plpgsql
security definer
set search_path to 'public'
as $fn$
declare
  v_started timestamptz := clock_timestamp();
  v_ceiling timestamptz;
  v_state   timestamptz;
  v_from    timestamptz;
  v_rows    integer := 0;
begin
  -- Transaction-scoped, so it is pooler-safe: a session-scoped advisory lock can outlive
  -- the statement on a transaction pooler and never be released. Returns -1 (not an
  -- error) when a manual repair and a cron tick collide; pg_cron never overlaps a job
  -- with itself, so in normal operation this is uncontended.
  if not pg_try_advisory_xact_lock(hashtext('refresh_llm_cost_rollups')::bigint) then
    return -1;
  end if;

  -- CLOSED hours only.
  v_ceiling := date_trunc('hour', now() at time zone 'UTC') at time zone 'UTC';

  select s.complete_through into v_state from llm_cost_rollup_state s where s.id;
  v_state := coalesce(v_state, '-infinity');

  -- LEAST, not GREATEST. p_from is a "start no LATER than" override: '-infinity' is the
  -- full repair, NULL is the mandatory 3-hour trailing re-scan, and no caller can shrink
  -- that re-scan. GREATEST discards '-infinity' entirely -- verified,
  -- greatest('-infinity', '2026-08-25 00:00+00') = 2026-08-25 00:00+00 -- which would
  -- have made the backfill below and every "full repair" a silent no-op.
  v_from := least(coalesce(p_from, 'infinity'::timestamptz),
                  case when v_state = '-infinity' then '-infinity'::timestamptz
                       else v_state - interval '3 hours' end);

  if v_from >= v_ceiling then
    return 0;                                   -- no closed hour to (re)compute
  end if;

  -- A FULL recompute of every touched bucket, NEVER a delta: re-running any window any
  -- number of times in any order converges to the same table.
  --
  -- The 3-hour trailing re-scan is what absorbs late arrivals. called_at defaults to
  -- now() = TRANSACTION START, so a call whose transaction opened at 10:59:59 and
  -- committed at 11:00:05 is invisible to a refresh that snapshotted at 11:00:00 even
  -- though its called_at sits in the already-closed hour 10. The next tick repairs it
  -- because this is a full recompute. Never a double-count; never a drop that outlives
  -- one tick. This holds only while no transaction stays open longer than the window --
  -- measured, the oldest open transaction on this instance was 28.7 minutes, 6x inside
  -- the margin. scripts/verify_pipeline.py check_long_open_transaction watches that.
  insert into llm_cost_hour_rollup as r (
    bucket_hour, called_for, provider, model,
    calls, error_calls, cost_usd,
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
  select (date_trunc('hour', l.called_at at time zone 'UTC') at time zone 'UTC'),
         l.called_for, l.provider, l.model,
         count(*)::integer,
         count(*) filter (where l.error is not null)::integer,
         sum(l.cost_usd),                       -- UNROUNDED
         sum(l.input_tokens)::bigint,
         sum(l.output_tokens)::bigint,
         sum(l.cache_read_tokens)::bigint,
         sum(l.cache_write_tokens)::bigint
    from llm_calls l
   where l.called_at >= v_from
     and l.called_at <  v_ceiling
   group by 1, 2, 3, 4
   order by 1, 2, 3, 4                          -- deterministic row-lock order
  on conflict (bucket_hour, called_for, provider, model) do update
     set calls              = excluded.calls,
         error_calls        = excluded.error_calls,
         cost_usd           = excluded.cost_usd,
         input_tokens       = excluded.input_tokens,
         output_tokens      = excluded.output_tokens,
         cache_read_tokens  = excluded.cache_read_tokens,
         cache_write_tokens = excluded.cache_write_tokens;
  get diagnostics v_rows = row_count;

  -- ON CONFLICT never REMOVES. Without this, a rollup group whose source rows vanished
  -- survives forever and "converges" is simply false. This is not hypothetical in CI:
  -- the schema-replay suite really does delete llm_calls rows.
  delete from llm_cost_hour_rollup r
   where r.bucket_hour >= v_from
     and r.bucket_hour <  v_ceiling
     and not exists (
       select 1 from llm_calls l
        where l.called_at >= r.bucket_hour
          and l.called_at <  r.bucket_hour + interval '1 hour'
          and l.called_for = r.called_for
          and l.provider   = r.provider
          and l.model      = r.model);

  -- UNCONDITIONAL. During the credit outage most hours have zero calls; if the watermark
  -- only advanced when rows were written it would never move, and the live edge would
  -- grow without bound while the page still looked fine.
  update llm_cost_rollup_state set complete_through = v_ceiling where id;

  update derived_artifacts
     set last_succeeded_at = clock_timestamp(),
         last_duration_ms  = (extract(epoch from clock_timestamp() - v_started) * 1000)::integer,
         last_rows         = v_rows,
         complete_through  = v_ceiling
   where name = 'llm_cost_hour_rollup';

  return v_rows;
end
$fn$;

revoke all on function public.refresh_llm_cost_rollups(timestamptz) from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- 7. The spend guard's "today" -- the third and last place the zone is spelled
-- ---------------------------------------------------------------------------
--
-- ci-allow-ungated: llm_cost_today_usd -- EXECUTE is revoked from every browser role
-- below; the only callers are the API's own postgres/service_role connection and the
-- operator.
--
-- SECURITY INVOKER on purpose: DEFINER here would be a standing read of total spend for
-- whoever ever acquires EXECUTE by accident. Reading through llm_cost_hour_union -- a
-- postgres-owned, NON-security_invoker view -- is what lets an invoker call see through
-- the RLS wall on the rollup. If anyone ever flips that view to security_invoker (a
-- change that looks like a security improvement), the rollup branch returns zero rows
-- for any caller without BYPASSRLS, this returns only the live edge, and the spend guard
-- goes silently deaf with NO exception raised for the caller's try/except to catch.
-- Rail: tests/test_llm_cost_rollup_indexable.py::test_today_usd_matches_the_daily_view.
--
-- It reads the rollup rather than the source because it cannot stay on llm_calls:
-- llm_calls_utc_day_rollup_idx is built on a UTC day and a Prague-day predicate cannot
-- match it. The alternative was a fourth zone literal as a new Prague-day expression
-- index on the hot insert path, adding write amplification to every recorded LLM call.
create or replace function public.llm_cost_today_usd()
returns numeric
language sql
stable
security invoker
set search_path to 'public'
as $fn$
  select round(coalesce(sum(u.cost_usd), 0), 4)
    from public.llm_cost_hour_union u
   where (u.bucket at time zone 'Europe/Prague')::date
       = (now()    at time zone 'Europe/Prague')::date
$fn$;

revoke all on function public.llm_cost_today_usd() from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- 8. Backfill
-- ---------------------------------------------------------------------------
-- The only expensive statement here: the whole-table aggregate measured at 36,544
-- shared blocks plus ~3,200 one-hour index probes for the anti-join pass. It runs
-- against an append-only table that nothing else writes at the boundary.
select public.refresh_llm_cost_rollups('-infinity');

commit;

-- ---------------------------------------------------------------------------
-- 9. The schedule
-- ---------------------------------------------------------------------------
-- 4,19,34,49 collides with NOTHING on the live board: jobid 1 fires at :00 :10 :20 :30
-- :40 :50, jobid 5 at :10, jobid 6 at :00 :15 :30 :45, jobid 7 at :07 :37, jobid 3 at
-- :30 every 6h. A plain */15 would land squarely on jobid 6, the instance's heaviest and
-- currently most fragile job. pg_cron task workers come from max_worker_processes = 6,
-- not from cron.max_running_jobs = 32, so avoiding shared tick boundaries is what
-- matters here, not the nominal slot ceiling.
--
-- Cost: 96 ticks/day at ~179 blocks / sub-10ms each (July-shape) is ~5 job-seconds/day.
-- W0b returned 34,399 job-seconds/day by deleting jobid 8; this spends 0.014 percent of
-- that. statement_timeout is ~1000x the measured work -- set so this job can never be
-- the thing that eats the scheduler, not because it needs the budget.
--
-- Guarded: the CI schema-replay container has no pg_cron, and an unguarded cron call
-- there fails the whole replay (the trap migration 432's revert hit).
do $cron$
begin
  perform cron.schedule(
    'llm-cost-rollup-refresh',
    '4,19,34,49 * * * *',
    $job$set statement_timeout='60s'; select public.refresh_llm_cost_rollups();$job$
  );
exception when others then
  raise notice 'pg_cron unavailable; llm-cost-rollup-refresh not scheduled (%).', sqlerrm;
end
$cron$;
