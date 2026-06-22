-- 220_workflow_failure_streaks.sql
--
-- Make failure monitoring STREAK-AWARE so a chronic break (every run failing for
-- days — e.g. enrich_bazos failed 84/84 runs for 3 weeks) is visually distinct
-- from a 1% self-healing transient (e.g. a detail_drain pooler blip). Migration
-- 178's workflow_failures is failure-ONLY, so "failures since the last success" —
-- the one signal that separates the two — was uncomputable, and the flat Health
-- card rendered both as identical reds (the alarm fatigue that hid the chronic
-- break for 3 weeks).
--
-- Added:
--   * workflow_failures.workflow_path — the STABLE workflow id (the Actions run
--     payload's `.path`, e.g. .github/workflows/enrich_bazos.yml). workflow_name
--     is free display text and forks history on a rename.
--   * workflow_run_health — one upserted row PER WORKFLOW tracking its latest
--     SUCCESS. NOT a success ledger: O(workflows) rows (~50). The poller already
--     fetched success runs (it filtered them out and discarded them); now it
--     records the latest success per workflow so the streak resets when a job
--     recovers. A page-computed streak would misread low-frequency chronic jobs
--     (the 200-run API page spans ~1h, so a 6-hourly job never shows >1 in-page
--     failure) — the state row is what makes "failures since last success"
--     correct regardless of cadence.
--   * workflow_failure_summary() — grouped one-row-per-workflow view with the
--     consecutive-failure streak and an is_chronic flag (streak >= 3).
--
-- recent_workflow_failures() (migration 178) is left in place — additive. The
-- frontend cuts over to the summary; the old RPC is dropped in a follow-up.

alter table workflow_failures
  add column if not exists workflow_path text;

create index if not exists idx_workflow_failures_path
  on workflow_failures (workflow_path);

create table if not exists workflow_run_health (
  workflow_path        text primary key,
  workflow_name        text,
  last_success_at      timestamptz,
  last_success_run_id  bigint,
  updated_at           timestamptz default now()
);

create or replace function public.workflow_failure_summary(p_hours int default 168)
 returns table(
   workflow_path        text,
   workflow_name        text,
   failure_count        bigint,
   first_failure_at     timestamptz,
   last_failure_at      timestamptz,
   last_conclusion      text,
   last_html_url        text,
   last_success_at      timestamptz,
   consecutive_failures bigint,
   is_chronic           boolean
 )
 language sql
 stable
 security definer
 set search_path = public
as $function$
  with win as (
    select wf.*
    from workflow_failures wf
    where wf.recorded_at > now() - make_interval(hours => p_hours)
      and wf.workflow_path is not null
  ),
  grouped as (
    select
      w.workflow_path,
      (array_agg(w.workflow_name order by w.run_started_at desc nulls last))[1] as workflow_name,
      count(*)                                                               as failure_count,
      min(w.run_started_at)                                                  as first_failure_at,
      max(w.run_started_at)                                                  as last_failure_at,
      (array_agg(w.conclusion order by w.run_started_at desc nulls last))[1] as last_conclusion,
      (array_agg(w.html_url   order by w.run_started_at desc nulls last))[1] as last_html_url
    from win w
    group by w.workflow_path
  )
  select
    g.workflow_path,
    g.workflow_name,
    g.failure_count,
    g.first_failure_at,
    g.last_failure_at,
    g.last_conclusion,
    g.last_html_url,
    h.last_success_at,
    streak.consecutive_failures,
    (streak.consecutive_failures >= 3) as is_chronic
  from grouped g
  left join workflow_run_health h on h.workflow_path = g.workflow_path
  cross join lateral (
    -- failures recorded after the last success (or all in-window if the job has
    -- no recorded success), so the streak resets the moment a job recovers.
    select count(*) as consecutive_failures
    from win w2
    where w2.workflow_path = g.workflow_path
      and (h.last_success_at is null or w2.run_started_at > h.last_success_at)
  ) streak
  order by is_chronic desc, streak.consecutive_failures desc, g.last_failure_at desc;
$function$;

grant execute on function public.workflow_failure_summary(int) to anon, authenticated;
