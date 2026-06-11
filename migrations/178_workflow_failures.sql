-- 178_workflow_failures.sql
--
-- GitHub Actions failures are invisible unless the operator opens the Actions
-- tab: GitHub only emails about failed SCHEDULED runs, and the (since-fixed)
-- image-backfill red loop trained everyone to ignore those mails anyway. A
-- 30-minute poller workflow (monitor_workflow_failures.yml →
-- scripts/record_workflow_failures.py) records every failed run here; the
-- Health page lists the last 48 h via the SECURITY DEFINER RPC below — no
-- GitHub token in the browser, nothing exposed beyond name/conclusion/time/link.

create table if not exists workflow_failures (
  id bigserial primary key,
  run_id bigint unique,
  workflow_name text not null,
  conclusion text not null,
  run_started_at timestamptz,
  html_url text,
  recorded_at timestamptz default now()
);

create index if not exists idx_workflow_failures_recorded_at
  on workflow_failures (recorded_at);

create or replace function public.recent_workflow_failures(p_hours int default 48)
 returns table(workflow_name text, conclusion text, run_started_at timestamptz, html_url text)
 language sql
 stable
 security definer
 set search_path = public
as $function$
  select workflow_name, conclusion, run_started_at, html_url
  from workflow_failures
  where recorded_at > now() - make_interval(hours => p_hours)
  order by run_started_at desc nulls last
$function$;

grant execute on function public.recent_workflow_failures(int) to anon, authenticated;
