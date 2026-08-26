-- 441: the registry stops declaring eleven artifacts it cannot observe.
--
-- Migration 440 seeded `public.derived_artifacts` to 14 rows and emptied the W7 backlog, so
-- the platform now DECLARES a producer, a cadence and a staleness budget for every derived
-- artifact. Corollary E has a second half -- "and its freshness is OBSERVABLE" -- and 440 did
-- not deliver it: only three producers stamp (`rebuild_browse_list`,
-- `rebuild_properties_map_mv`, `refresh_llm_cost_rollups`). Verified live 2026-08-26 before
-- this migration:
--
--   select count(*) from public.derived_artifacts where last_succeeded_at is null;  -- => 11
--
-- Eleven permanently-NULL rows is not a neutral state. A Health panel with eleven rows that
-- read "never" trains the operator to stop reading the panel, which costs more than having
-- no panel. For two of them there is no other signal at all: a NON-CONCURRENT `refresh
-- materialized view` swaps the heap, so `pg_stat_user_tables.last_autoanalyze`/n_tup_* reset
-- to zero and `pg_stat_file` is denied on this instance -- `price_stat_choropleth` and
-- `rent_map_choropleth` are literally unobservable without a stamp.
--
-- ---------------------------------------------------------------------------------------
-- ONE HELPER, NOT FIVE INSTRUMENTATIONS.
--
-- `public.stamp_derived_artifact(name, rows, duration_ms)` is the whole mechanism. Five
-- producers in three languages (plpgsql, Python-on-Actions, Python-in-a-request-handler) call
-- the same function, so the registry has exactly one write shape and adding the sixth
-- producer is one line rather than one more hand-rolled UPDATE to get subtly wrong.
--
-- WHY SECURITY DEFINER (the decision this header owes a justification for). Measured live on
-- this instance, 2026-08-26:
--     derived_artifacts owner            = postgres
--     relrowsecurity                     = true
--     relforcerowsecurity                = false
--     policies on the table              = 0
--     has_table_privilege(service_role)  = UPDATE true, rolbypassrls = true
--     has_table_privilege(authenticated) = UPDATE false
--     has_table_privilege(anon)          = UPDATE false
-- So SECURITY INVOKER would in fact work TODAY for both live caller classes: pg_cron's
-- functions run as the postgres owner (which bypasses RLS because FORCE is off) and the
-- Python producers connect as service-role (which has both the grant and BYPASSRLS).
-- DEFINER is chosen anyway because of the failure mode, not the happy path. This UPDATE is
-- deliberately allowed to match nothing (see below), so ANY future loss of write access
-- turns every stamp into a silent no-op that looks exactly like "the producer never ran" --
-- the precise illusion this wave exists to remove. Two realistic ways to get there:
-- `alter table public.derived_artifacts force row level security` (a change that reads like a
-- security improvement, and with zero policies would then deny the owner too), or a producer
-- moved onto the tenant pool / an `authenticated` connection. Under DEFINER the stamp keeps
-- working through both and the callable surface is closed by the REVOKE below instead. It
-- also matches the three producers that already write this table -- all three are SECURITY
-- DEFINER and all three write it as postgres -- so there is one privilege story, not two.
--
-- `set search_path = public` is mandatory hygiene for any SECURITY DEFINER function and is
-- what makes the unqualified `derived_artifacts` in the body unspoofable.
--
-- THE REVOKE NEEDS `public` AS WELL AS THE TWO NAMED ROLES. PostgreSQL grants EXECUTE on a
-- new function to PUBLIC by default (built in, not an ACL entry), and this project's
-- pg_default_acl additionally names anon/authenticated explicitly -- verified live:
--     select defaclacl from pg_default_acl
--      where defaclnamespace = 'public'::regnamespace and defaclobjtype = 'f';
--     => {postgres=X/supabase_admin,anon=X/supabase_admin,authenticated=X/...,...}
-- Revoking only anon + authenticated would therefore leave the function callable by every
-- role through the PUBLIC grant, PostgREST RPC included. This is the same trap migration 287
-- documented. Rail: tests/test_derived_artifacts_stamping.py.
--
-- ---------------------------------------------------------------------------------------
-- THE UPDATE MATCHING NOTHING IS DELIBERATE, AND SO IS THE RAIL THAT COMPENSATES.
--
-- A missing row must not be an error. Migration 440's own header states the reason for its
-- inline version and it holds for all five callers here: the alternative (`get diagnostics` +
-- `raise`) would put a live read model, a scheduled sweep or a FastAPI request behind the
-- existence of one metadata row -- deleting a registry row would take Browse down inside 15
-- minutes and 500 an ingest endpoint. An `insert ... on conflict do update` would be
-- self-healing but would force every caller to spell producer/host/cadence/budget, giving the
-- registry two sources of truth.
--
-- The cost of that choice is real and it is stated here rather than glossed: a MISTYPED name
-- stamps nothing, forever, and reads on the panel as a dead artifact -- indistinguishable
-- from the condition it is supposed to detect. That hole is closed OFFLINE, in CI, by
-- tests/test_derived_artifacts_stamping.py, which extracts every artifact-name literal passed
-- to `stamp_derived_artifact` from BOTH the repo tree AND `pg_proc.prosrc` and requires each
-- one to exist in `derived_artifacts`. Do not "improve" this function by making it raise.
--
-- ---------------------------------------------------------------------------------------
-- THE HEALTH FAN-OUT WAS NOT HAND-RETYPED, and the diff is small enough to prove inline.
--
-- Migration 371 rebuilt `rebuild_browse_list` from an outdated body and silently reintroduced
-- an `anon` grant plus migration 277's narrow indexes while its commit message claimed no
-- behaviour change. Migration 440 answered that by capturing from the live catalog and
-- publishing hashes. Same rule here. `refresh_health_matviews` was captured with
-- `pg_get_functiondef` from the LIVE catalog on erlvtprrmrylhznfyaih, 2026-08-26:
--
--     md5(prosrc)                0ba891c6126016da31cc3acebbd0170a   (389 chars)
--     md5(pg_get_functiondef)    3799e0d3561570bc5583bbd737997bb8
--     proconfig                  {search_path=public}   -- NO statement_timeout; the 300s
--                                budget is armed by the cron command, migration 371
--
-- NO DRIFT. Unlike the two rebuild functions in 440, this body is byte-identical to the
-- newest on-disk definition (migration 371, lines 167-181) -- verified by extracting the
-- dollar-quoted body from the file and hashing it: same 389 chars, same md5. The body is
-- short enough to reproduce here in full, which is a stronger check than a hash, so a
-- reviewer can diff the six added lines by eye:
--
--     begin
--       refresh materialized view concurrently health_summary_mv;
--       refresh materialized view concurrently portal_health_mv;
--       refresh materialized view concurrently snapshot_churn_24h_mv;
--       refresh materialized view concurrently scraper_health_checks_mv;
--       refresh materialized view concurrently category_trends_mv;
--       refresh materialized view concurrently health_mv_refresh_stamp;
--     end;
--
-- (Note the REAL order, since migration 440's own comment records a different one:
-- snapshot_churn_24h_mv is THIRD and scraper_health_checks_mv FOURTH. 440 only used the order
-- decoratively -- all six rows carry identical cadence and budget -- but the stamps below
-- must follow the catalog, not the comment.)
--
-- WHAT IS ADDED, exhaustively: a `declare t timestamptz;`, six `t := clock_timestamp();`
-- assignments, and six `perform public.stamp_derived_artifact(...)` calls. Nothing is
-- removed, reordered or reworded. No error handling is added -- deliberately, and it is worth
-- being explicit because the two look adjacent: stamping the six matviews INDIVIDUALLY is not
-- the same change as giving the fan-out per-matview error handling, which is filed separately
-- and is NOT in this migration.
--
-- WHY SIX STAMPS AND NOT ONE AT THE END. Two reasons, and only the second is true today:
--   (a) Aspirational. If the fan-out is ever split, or given per-item handling, a partial run
--       leaves the matviews that DID refresh correctly stamped and the rest visibly stale.
--       HONEST CORRECTION, because the wave brief asserted this as the present-tense reason
--       and it is not: today all six REFRESHes and all six stamps share ONE transaction (a
--       plpgsql function without a BEGIN..EXCEPTION block opens no subtransaction), so a
--       failure at the fourth refresh -- or the cron command's 300s statement_timeout firing
--       -- rolls back the three that succeeded ALONG WITH their stamps. There is nothing
--       left "correctly stamped" to preserve. Six stamps are still the right shape; they are
--       not yet load-bearing for this reason.
--   (b) LOAD-BEARING TODAY: `last_duration_ms` becomes six real per-matview numbers instead
--       of one batch total. `clock_timestamp()` advances inside a transaction (unlike
--       `now()`), so each stamp measures its own REFRESH. Migration 440 recorded that the
--       refresh gap's trailing-7d p95 is 77.7 minutes against a 90d p95 of 10.0 -- i.e. the
--       job is in a degraded regime -- and nothing published anywhere says WHICH of the six
--       is slow. After this, the registry does.
--
-- `last_rows` is deliberately left NULL for all six. Filling it needs a `select count(*)` per
-- matview inside a job already running against a 300s budget in a degraded regime; buying a
-- cosmetic column with real work on the slowest job on the instance is the wrong trade.
--
-- ci-allow-ungated: refresh_health_matviews -- unchanged posture from migration 371, whose
-- annotation this repeats verbatim in substance: the function only issues REFRESH
-- MATERIALIZED VIEW (plus, now, stamps) and returns void; it exposes no admin-only row to any
-- caller, and EXECUTE on it is held by pg_cron and the operator. The scanner flags it only
-- because five of the six matview NAMES it refreshes are in _ADMIN_ONLY_RELATIONS.
--
-- ---------------------------------------------------------------------------------------
-- WHAT THIS MIGRATION DOES NOT DO. The other four artifacts' producers are Python
-- (scripts/resolve_brokers.py, scraper/price_stats_db.py, api/rent_map.py,
-- scripts/refresh_image_stats.py) and are instrumented in the same change, in code, calling
-- this same function. They run on GitHub Actions / Railway schedules, so their rows advance
-- on their own cadence rather than at apply time. Rails:
-- tests/test_derived_artifacts_stamping.py (names + producers + the six individual stamps)
-- and tests/test_sql_schema_prepare.py (the helper call PREPAREs against the replayed
-- schema).
-- ---------------------------------------------------------------------------------------

begin;

-- `refresh_health_matviews` is replaced below and a tick that is MID-EXECUTION holds a lock
-- on its own pg_proc entry, so without this the CREATE OR REPLACE queues behind a refresh
-- (up to the job's 300s statement_timeout) while holding every lock this transaction has
-- already taken. Fail fast and retry in a quiet window instead. Same reasoning as 440.
set local lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 1. The helper
-- ---------------------------------------------------------------------------
--
-- `complete_through` = `last_succeeded_at` for every caller of this function: all five are
-- FULL rebuilds of their artifact (a `refresh materialized view`, concurrent or not, has no
-- partial mode), and the stamp is written at COMPLETION, so the artifact is complete through
-- the moment it succeeded. The one artifact where the two genuinely differ --
-- `llm_cost_hour_rollup`, which is incremental and carries a watermark -- keeps its own
-- inline UPDATE in migration 437 and does NOT call this helper, precisely because it must
-- pass its own ceiling as `complete_through`. Do not "unify" it onto this function without
-- adding a fourth parameter.
--
-- COALESCE, not assignment: passing NULL for rows/duration means "I did not measure this",
-- which must leave the previously-observed value alone rather than erase it. The stamp is
-- the only writer of these two columns, so a caller that never measures simply leaves them
-- NULL forever, which is honest.
create or replace function public.stamp_derived_artifact(
  p_name        text,
  p_rows        bigint  default null,
  p_duration_ms integer default null
)
returns void
language sql
security definer
set search_path = public
as $fn$
  update public.derived_artifacts
     set last_succeeded_at = clock_timestamp(),
         complete_through  = clock_timestamp(),
         last_rows         = coalesce(p_rows, last_rows),
         last_duration_ms  = coalesce(p_duration_ms, last_duration_ms)
   where name = p_name;
$fn$;

comment on function public.stamp_derived_artifact(text, bigint, integer) is
  'Record a successful production of one derived artifact (Corollary E). Deliberately a '
  'silent no-op when the name is unregistered -- a producer must never fail because a '
  'metadata row is missing; typos are caught in CI by '
  'tests/test_derived_artifacts_stamping.py.';

-- `public` first: PostgreSQL's built-in default grants EXECUTE on every new function to
-- PUBLIC, so revoking only the two named roles leaves it callable by everyone.
revoke all on function public.stamp_derived_artifact(text, bigint, integer)
  from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- 2. The health fan-out -- live body, stamp calls and their timers only
-- ---------------------------------------------------------------------------
create or replace function public.refresh_health_matviews()
returns void
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
  t timestamptz;
begin
  t := clock_timestamp();
  refresh materialized view concurrently health_summary_mv;
  perform public.stamp_derived_artifact('health_summary_mv', null,
    (extract(epoch from clock_timestamp() - t) * 1000)::integer);

  t := clock_timestamp();
  refresh materialized view concurrently portal_health_mv;
  perform public.stamp_derived_artifact('portal_health_mv', null,
    (extract(epoch from clock_timestamp() - t) * 1000)::integer);

  t := clock_timestamp();
  refresh materialized view concurrently snapshot_churn_24h_mv;
  perform public.stamp_derived_artifact('snapshot_churn_24h_mv', null,
    (extract(epoch from clock_timestamp() - t) * 1000)::integer);

  t := clock_timestamp();
  refresh materialized view concurrently scraper_health_checks_mv;
  perform public.stamp_derived_artifact('scraper_health_checks_mv', null,
    (extract(epoch from clock_timestamp() - t) * 1000)::integer);

  t := clock_timestamp();
  refresh materialized view concurrently category_trends_mv;
  perform public.stamp_derived_artifact('category_trends_mv', null,
    (extract(epoch from clock_timestamp() - t) * 1000)::integer);

  t := clock_timestamp();
  refresh materialized view concurrently health_mv_refresh_stamp;
  perform public.stamp_derived_artifact('health_mv_refresh_stamp', null,
    (extract(epoch from clock_timestamp() - t) * 1000)::integer);
end;
$function$;

commit;

-- ---------------------------------------------------------------------------------------
-- VERIFY AFTER APPLYING:
--
--   -- the helper exists, is DEFINER, and no browser role can call it
--   select prosecdef, proconfig,
--          has_function_privilege('anon',          p.oid, 'EXECUTE'),
--          has_function_privilege('authenticated', p.oid, 'EXECUTE')
--     from pg_proc p where proname = 'stamp_derived_artifact';   -- => t, {search_path=public}, f, f
--
--   -- all six stamps are inside the fan-out, each after its own refresh
--   select count(*) from regexp_matches(
--            (select prosrc from pg_proc where proname = 'refresh_health_matviews'),
--            'stamp_derived_artifact\(''([a-z0-9_]+)''', 'g');    -- => 6
--
--   -- and then, after one `*/10` tick, the six rows carry a stamp
--   select name, last_succeeded_at, last_duration_ms from public.derived_artifacts
--    where producer = 'refresh_health_matviews' order by name;
-- ---------------------------------------------------------------------------------------
