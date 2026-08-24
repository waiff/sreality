-- 421: make the /costs rollups indexable — and pin their bucket boundaries to UTC.
--
-- Both views bucket `llm_calls.called_at` (timestamptz) with an expression that is only
-- STABLE, not IMMUTABLE, because casting/truncating a timestamptz depends on the session
-- TimeZone. Postgres therefore REFUSES to index them:
--
--   create index on llm_calls (((called_at)::date));
--   ERROR 42P17: functions in index expression must be marked IMMUTABLE
--
-- So the "date-expression index" this wave wanted could not be built at all until the
-- expression itself was pinned to an explicit zone. Doing that is a correctness fix in its
-- own right: the day/hour a call was attributed to silently depended on whatever TimeZone
-- the reading session carried, so the same row could land in different buckets for
-- different readers. The database runs UTC today and the rewrite is a no-op on live data —
-- verified across all 293,551 rows, zero disagreements for both expressions, and the
-- daily rollup returns an identical 93 groups / 62,362 calls / 53,842 errors / $20.7555
-- before and after.
--
-- Measured live, `EXPLAIN (ANALYZE, BUFFERS)`, both at the windows the page actually asks
-- for (35 days daily, 49 hours hourly):
--
--   daily   before: Seq Scan, 231,189 rows discarded for 93 out,
--                   9,711 shared + 5,839 temp blocks (23 MB external merge sort)
--           after:  Index Scan in group order, no sort at all,
--                   10,851 shared + 0 temp blocks
--   hourly  before: Seq Scan, 293,491 rows discarded for 12 out, 9,941 blocks
--           after:  Index Scan, 60 rows read for 12 out, 8 blocks  (~1,240x)
--
-- The hourly view is where the pathology was worst — it scanned the entire table to answer
-- a two-day chart — and it is now proportional to what renders. The daily view improves
-- more modestly because 35 days is ~21% of the table; its win is structural (the external
-- merge sort to disk is gone entirely, since the index already delivers the GROUP BY order).
--
-- Index shape: key on the bucket expression, then the remaining GROUP BY columns in order,
-- so GroupAggregate consumes the scan directly and never sorts. Deliberately NO `include`
-- list — a covering variant measured *worse* (11,529 blocks vs 10,851) and 13x larger
-- (29 MB vs 2.2 MB), because an Index Only Scan is not reachable here: the planner does not
-- match this expression index for index-only, and on an append-only table the newest heap
-- pages — exactly the ones a recency query reads — are the least likely to be all-visible.
-- Paying for an INCLUDE that can never be used is pure write amplification.
--
-- Both views stay SECURITY DEFINER (reloptions NULL, no security_invoker) gated by
-- `is_platform_admin()` in the outer WHERE — they are admin-only reporting views over a
-- shared table, NOT per-account RLS views, so invoker mode would be wrong here, not safer.
-- `create or replace view` preserves the existing ACL; the grants below re-assert it
-- explicitly anyway (live: `authenticated` SELECT, `anon` dark, `service_role` full).

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

-- `bucket` must stay timestamptz: the SPA parses it with `new Date(...)`, and a bare
-- timestamp would be read as browser-local time and shift the whole chart. Truncating in
-- UTC and labelling the result UTC keeps both the type and every value identical.
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

create index if not exists llm_calls_utc_day_rollup_idx
  on public.llm_calls (((called_at at time zone 'UTC')::date), called_for, provider, model);

create index if not exists llm_calls_utc_hour_rollup_idx
  on public.llm_calls (
    ((date_trunc('hour', called_at at time zone 'UTC') at time zone 'UTC')),
    called_for, provider, model
  );

revoke all on public.llm_cost_daily_public from anon;
revoke all on public.llm_cost_hourly_public from anon;
grant select on public.llm_cost_daily_public to authenticated;
grant select on public.llm_cost_hourly_public to authenticated;
