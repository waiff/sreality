-- Revert of 437 (llm cost hour rollup + derived-artifacts registry).
--
-- NOT APPLIED BY CI. Reverts live in migrations/reverts/ because the CI schema replay
-- runs `ls migrations/*.sql` non-recursively, and because test_migration_numbers.py
-- forbids duplicate numbers above 304. This file exists to be run by hand.
--
-- ORDER MATTERS. The two public views are restored to migration 421's bodies FIRST, so
-- they stop depending on llm_cost_hour_union before anything is dropped. Dropping in the
-- other order fails on the dependency and leaves /costs pointing at a half-reverted
-- schema.
--
-- WHAT IS DELIBERATELY LEFT IN PLACE: `derived_artifacts` and `derived_artifacts_public`.
-- They are additive, nothing else depends on the rollup for them to be correct, and the
-- browse_list / properties_map_mv rows they publish come from browse_read_model_state,
-- which this migration never touched. Only the llm_cost_hour_rollup registry row is
-- removed. Also left in place: migration 421's two indexes on llm_calls — they go to
-- zero scans under 437 but the restored views below need them, which is exactly why 437
-- did not drop them.

begin;

create or replace view public.llm_cost_daily_public as
select day, called_for, provider, model, calls, error_calls, cost_usd,
       input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
from (
  select (l.called_at at time zone 'UTC')::date as day,
         l.called_for,
         l.provider,
         l.model,
         count(*)::integer as calls,
         count(*) filter (where l.error is not null)::integer as error_calls,
         round(sum(l.cost_usd), 4) as cost_usd,
         sum(l.input_tokens) as input_tokens,
         sum(l.output_tokens) as output_tokens,
         sum(l.cache_read_tokens) as cache_read_tokens,
         sum(l.cache_write_tokens) as cache_write_tokens
  from llm_calls l
  group by ((l.called_at at time zone 'UTC')::date), l.called_for, l.provider, l.model
) __admin_gate
where is_platform_admin();

create or replace view public.llm_cost_hourly_public as
select bucket, called_for, provider, model, calls, error_calls, cost_usd,
       input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
from (
  select (date_trunc('hour', l.called_at at time zone 'UTC') at time zone 'UTC') as bucket,
         l.called_for,
         l.provider,
         l.model,
         count(*)::integer as calls,
         count(*) filter (where l.error is not null)::integer as error_calls,
         round(sum(l.cost_usd), 4) as cost_usd,
         sum(l.input_tokens) as input_tokens,
         sum(l.output_tokens) as output_tokens,
         sum(l.cache_read_tokens) as cache_read_tokens,
         sum(l.cache_write_tokens) as cache_write_tokens
  from llm_calls l
  group by (date_trunc('hour', l.called_at at time zone 'UTC') at time zone 'UTC'),
           l.called_for, l.provider, l.model
) __admin_gate
where is_platform_admin();

revoke all on public.llm_cost_daily_public  from anon;
revoke all on public.llm_cost_hourly_public from anon;
grant select on public.llm_cost_daily_public  to authenticated;
grant select on public.llm_cost_hourly_public to authenticated;

drop function if exists public.llm_cost_today_usd();
drop function if exists public.refresh_llm_cost_rollups(timestamptz);
drop view     if exists public.llm_cost_hour_union;
drop table    if exists public.llm_cost_hour_rollup;
drop table    if exists public.llm_cost_rollup_state;

delete from public.derived_artifacts where name = 'llm_cost_hour_rollup';

commit;

-- Guarded exactly as the forward migration is: the bare name `cron.unschedule` is
-- AMBIGUOUS across the bigint and text overloads, so `to_regproc` returns NULL for it and
-- a guard written that way never fires — the trap that left migration 432's job alive
-- against dropped objects. `to_regprocedure` with the full argument list is the spelling
-- that actually resolves.
do $revert$
begin
  if to_regprocedure('cron.unschedule(text)') is not null then
    perform cron.unschedule('llm-cost-rollup-refresh');
  end if;
exception when others then
  raise notice 'pg_cron unavailable; llm-cost-rollup-refresh not unscheduled (%).', sqlerrm;
end
$revert$;

-- And revert api/llm_client.py's DAILY_COST_TODAY_SQL to migration 421's spelling:
--   SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls
--    WHERE (called_at AT TIME ZONE 'UTC')::date = (now() AT TIME ZONE 'UTC')::date
