-- 274_pipeline_verification.sql
--
-- Pipeline verification harness + in-app system alerts.
--
-- WHY: the dedup/scrape pipeline stalled silently for two days (2026-07 Anthropic
-- credit exhaustion, 38k+ failed LLM calls) and the ONLY alarm was a failing
-- GitHub Actions cron the operator missed. Market-wide "dedup debt" (~39,376
-- suspect unmerged byt property pairs) was invisible entirely. This migration is
-- the schema half of the fix: a scheduled verification job (scripts/verify_pipeline.py)
-- writes one pipeline_check_results row per health metric, and a red (`fail`) check
-- rings the EXISTING in-app notification bell via a system_health notification_dispatches
-- row — the same feed the SPA nav badge already polls.
--
-- Purely additive: a new results table + two anon-readable views, three CHECK
-- widenings (drop + re-add, superset-only so no row can violate), two nullable-safe
-- ALTERs on notification_dispatches, two app_settings seeds, and a SECURITY DEFINER
-- dead-man-switch function scheduled hourly via the exception-guarded pg_cron pattern
-- (migration 136 precedent) so it still applies where pg_cron is absent.

begin;

-- 1. Results table ----------------------------------------------------------

create table pipeline_check_results (
  id         bigserial primary key,
  run_at     timestamptz not null,
  check_key  text not null,
  status     text not null check (status in ('ok', 'warn', 'fail')),
  value      numeric,
  details    jsonb,
  created_at timestamptz not null default now()
);

create index pipeline_check_results_key_run_idx
  on pipeline_check_results (check_key, run_at desc);

-- Service-role writes only. No anon policy => RLS denies anon on the TABLE; the
-- browser reads the views below (same posture as the dedup run tables).
alter table pipeline_check_results enable row level security;

comment on table pipeline_check_results is
  'One row per pipeline-health check per verify_pipeline run (scripts/verify_pipeline.py). '
  'Service-role writes; the browser reads pipeline_checks_public / pipeline_check_history_public.';

-- 2. Public read views ------------------------------------------------------

-- Latest row per check_key — the /health dashboard current state.
create view pipeline_checks_public as
  select distinct on (check_key)
         check_key, run_at, status, value, details, created_at
  from pipeline_check_results
  order by check_key, run_at desc;

grant select on pipeline_checks_public to anon;

-- The trailing 30 days of every check — trend sparklines.
create view pipeline_check_history_public as
  select check_key, run_at, status, value, details
  from pipeline_check_results
  where run_at > now() - interval '30 days';

grant select on pipeline_check_history_public to anon;

-- 3. Notification widening (system_health as a 3rd producer) -----------------

-- sreality_id becomes nullable: a system_health alert is not about any one listing.
alter table notification_dispatches
  alter column sreality_id drop not null;

-- Verbatim alert text for source_kind='system_health' rows (the feed / outbox
-- render it directly rather than composing from listing fields).
alter table notification_dispatches
  add column if not exists message text;

-- Add the system_health branch to the source FK check. PRESERVES the existing
-- watchdog + collection_monitor branches exactly (migration 206); a system_health
-- row carries neither a subscription nor a collection.
alter table notification_dispatches
  drop constraint if exists notification_dispatches_source_ck;
alter table notification_dispatches
  add constraint notification_dispatches_source_ck check (
    (source_kind = 'watchdog'           and subscription_id is not null and collection_id is null)
 or (source_kind = 'collection_monitor' and collection_id   is not null and subscription_id is null)
 or (source_kind = 'system_health'      and subscription_id is null     and collection_id is null)
  );

-- Add 'system_alert' to the change_kind set (superset of migration 209's values).
alter table notification_dispatches
  drop constraint if exists notification_dispatches_change_kind_ck;
alter table notification_dispatches
  add constraint notification_dispatches_change_kind_ck
  check (change_kind in (
    'new', 'price_drop', 'price_rise', 'inactive',
    'reactivated', 'new_source', 'broker_change',
    'system_alert'
  ));

-- Widen channel_sends so a future external delivery of a system_health alert is
-- valid. Two constraints gate a channel_sends row (migration 207): the consumer
-- VALUE set, and the origin-FK check. A system_health send is notification-backed
-- (a notification_dispatches row, like watchdog / collection_monitor), so it joins
-- that branch of BOTH — widening only the value set would leave the origin check
-- rejecting it at runtime. Both are supersets, so no existing row can violate.
alter table channel_sends
  drop constraint if exists channel_sends_consumer_check;
alter table channel_sends
  add constraint channel_sends_consumer_check
  check (consumer in ('watchdog', 'collection_monitor', 'outreach', 'system_health'));

alter table channel_sends
  drop constraint if exists channel_sends_check;
alter table channel_sends
  add constraint channel_sends_check check (
    (consumer in ('watchdog', 'collection_monitor', 'system_health')
       and notification_id is not null and outreach_message_id is null)
 or (consumer = 'outreach'
       and outreach_message_id is not null and notification_id is null)
  );

-- 4. Settings seeds ---------------------------------------------------------

insert into app_settings (key, value, description, updated_by) values
  ('pipeline_check_thresholds',
   jsonb_build_object(
     'street_debt_price_pct',       1.0,
     'street_debt_warn',            30000,
     'street_debt_fail',            45000,
     'geo_debt_area_pct',           20,
     'geo_debt_price_pct',          5,
     'merge_p95_warn_hours',        24,
     'unpublished_overdue_fail',    1,
     'cycle_age_fail_hours',        30,
     'dirty_age_p95_warn_hours',    6,
     'candidate_age_p95_warn_days', 14,
     'llm_error_rate_warn',         0.2,
     'verification_stale_hours',    24,
     'precision_sample_n',          15
   ),
   'Thresholds for the pipeline verification harness (scripts/verify_pipeline.py). '
   'Each check reads its keys here with the migration defaults as code fallbacks; '
   'operator-editable so a threshold change needs no deploy.',
   'migration_274'),
  ('system_health_channels', '[]'::jsonb,
   'External delivery channels for system_health alerts (email/telegram). Empty = '
   'in-app-only (the bell badge). toolkit.system_alerts.emit_system_alert stamps '
   'these into the dispatch target_channels.',
   'migration_274')
on conflict (key) do nothing;

-- 5. pg_cron dead-man switch ------------------------------------------------

-- Independent backstop: if the verify_pipeline cron itself dies (the exact failure
-- mode that hid the credit outage — a stuck Actions job), nothing writes new
-- pipeline_check_results rows, so a per-check alert can never fire. This in-DB
-- function alarms on that silence: when results exist but the freshest is older
-- than verification_stale_hours, it rings the same system_health bell. SECURITY
-- DEFINER so pg_cron (a low-privilege role) can insert.
create or replace function emit_verification_stale_alert()
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  stale_hours numeric;
  latest      timestamptz;
begin
  select coalesce((value ->> 'verification_stale_hours')::numeric, 24)
    into stale_hours
  from app_settings where key = 'pipeline_check_thresholds';
  stale_hours := coalesce(stale_hours, 24);

  select max(run_at) into latest from pipeline_check_results;
  if latest is null then
    return;  -- never run yet: nothing to be "stale" against.
  end if;

  if latest < now() - (stale_hours * interval '1 hour') then
    insert into notification_dispatches
      (source_kind, change_kind, channel, status, message, dedupe_key, target_channels)
    values (
      'system_health', 'system_alert', 'in_app', 'sent',
      'Pipeline verification is stale: the newest pipeline_check_results row is '
        || round(extract(epoch from (now() - latest)) / 3600.0)::text
        || 'h old (> ' || stale_hours::text || 'h). The verify_pipeline job may be '
        || 'stuck or failing — check Actions "Monitoring: pipeline verification".',
      'sys:verification_stale:' || to_char(now(), 'YYYY-MM-DD'),
      '{}'::text[]
    )
    on conflict (dedupe_key) do nothing;
  end if;
end;
$$;

comment on function emit_verification_stale_alert() is
  'Dead-man switch for the verification harness: rings the system_health bell once/day '
  'when pipeline_check_results has gone stale (the verify_pipeline cron itself died). '
  'Scheduled hourly via pg_cron.';

do $cron$
begin
  create extension if not exists pg_cron;
  perform cron.schedule(
    'emit-verification-stale-alert',
    '10 * * * *',
    $$select public.emit_verification_stale_alert();$$
  );
exception when others then
  raise notice 'pg_cron unavailable; verification-stale alert not scheduled (%). Run emit_verification_stale_alert() on another scheduler.', sqlerrm;
end
$cron$;

commit;
