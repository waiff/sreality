-- 422: /health's pipeline checks — read 15 rows by reading 15 rows.
--
-- `pipeline_checks_public` is a latest-row-per-check_key read: 15 keys out of a 6,234-row
-- append-only results table, and the page asks for ALL of them with no filter. The old body
-- was the textbook shape for that:
--
--   select distinct on (check_key) ... from pipeline_check_results
--   order by check_key, run_at desc
--
-- The supporting index already existed and was already being used — `(check_key, run_at DESC)`
-- — so there was no index left to add. That is the finding: DISTINCT ON still has to WALK
-- every index entry to find each group's boundary, so the plan read all 6,234 entries and
-- fetched all 6,234 heap tuples (the `details` jsonb makes them wide) to emit 15 rows.
-- Postgres 17 has no B-tree skip scan, so the planner cannot do better with this SQL.
--
-- The rewrite is the classic loose index scan: a recursive CTE hops key-to-key through the
-- index (16 descents, one per key plus the terminator), then one LATERAL `limit 1` per key
-- takes that key's newest row. Cost becomes proportional to the 15 rows rendered instead of
-- to the table's whole history — and it stays proportional as the table grows, which the old
-- shape did not: every future check run made the page's read strictly more expensive.
--
-- Measured live, `EXPLAIN (ANALYZE, BUFFERS)`, the exact statement the page issues:
--   before: Index Scan + Unique, 6,234 rows read for 15 out, 3,351 blocks (3,721 with the
--           admin gate), 5,023 ms
--   after:  Recursive Union + 15 LATERAL probes, 15 rows read for 15 out, 96 blocks, 23 ms
--   → ~35x fewer blocks
--
-- NOT bounded by time, deliberately. "Bound the history" was the obvious reading of this
-- target, but the UI does not ask for one: `fetchPipelineChecks` sends no filter, and
-- `pipelineChecks.ts` explicitly humanizes keys from RETIRED checks ("Historical rows from
-- retired checks ... fall through to the humanizer"), so a date window would silently drop
-- exactly the rows that comment exists to keep. The right bound here is "one row per key",
-- which is what the view always meant — it was just being computed the expensive way.
--
-- Semantics are preserved EXACTLY, including the tie-break. DISTINCT ON with
-- `order by check_key, run_at desc` breaks a (check_key, run_at) tie arbitrarily, and so
-- does `order by run_at desc limit 1`. Adding `, id desc` would make it deterministic, but
-- it also adds an Incremental Sort (measured: 96 -> 117 blocks) and would be a behaviour
-- change smuggled into a performance migration. Live check: **0 tied (check_key, run_at)
-- pairs exist**, so the question is moot today; if determinism is wanted it belongs in its
-- own change. Equivalence verified live before applying: 15 rows both ways, and set-compared
-- across every column including the `details` jsonb — **0 rows in either direction**.
--
-- Unchanged: SECURITY DEFINER (reloptions NULL) with the `is_platform_admin()` outer gate,
-- which stays a One-Time Filter above the CTE so a non-admin never executes it. Live ACL is
-- `authenticated` SELECT, `anon` dark, `service_role` full; `create or replace view`
-- preserves it and the grants below re-assert it anyway.

create or replace view public.pipeline_checks_public as
select check_key, run_at, status, value, details, created_at
from (
  with recursive keys as (
    -- Lowest key in the index...
    (select r.check_key
       from pipeline_check_results r
      order by r.check_key
      limit 1)
    union all
    -- ...then repeatedly seek to the next key strictly greater than the last. Emits one
    -- trailing NULL when the last key is reached; the outer WHERE drops it.
    (select (select r.check_key
               from pipeline_check_results r
              where r.check_key > k.check_key
              order by r.check_key
              limit 1)
       from keys k
      where k.check_key is not null)
  )
  select latest.check_key,
         latest.run_at,
         latest.status,
         latest.value,
         latest.details,
         latest.created_at
  from keys k
  cross join lateral (
    select c.check_key, c.run_at, c.status, c.value, c.details, c.created_at
      from pipeline_check_results c
     where c.check_key = k.check_key
     order by c.run_at desc
     limit 1
  ) latest
  where k.check_key is not null
) __admin_gate
where is_platform_admin();

revoke all on public.pipeline_checks_public from anon;
grant select on public.pipeline_checks_public to authenticated;
