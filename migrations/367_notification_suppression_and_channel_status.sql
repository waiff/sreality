-- 367_notification_suppression_and_channel_status.sql
--
-- Wave 3 (watchdogs & notifications), public-delivery-safety axis. Ships DARK
-- (the outbox only runs when a transport is configured; the webhook 503s without
-- its secret). Three pieces:
--
-- 1. notification_suppression — GLOBAL address-level suppression. Mirrors
--    broker_outreach_suppression's global, deletion-surviving semantics, but keyed
--    on (channel, address) since notifications target addresses, not broker_ids. A
--    bounce / complaint / one-click unsubscribe suppresses that address on that
--    channel EVERYWHERE and survives tenant deletion — a hard requirement for
--    CAN-SPAM/GDPR-safe transactional email. The outbox checks it pre-send.
-- 2. channel_sends.status widen — +delivered/bounced/complained (Resend webhook
--    feedback) +suppressed (the pre-send gate). Foreseen verbatim at migration 207.
-- 3. resend_webhook_events — the webhook idempotency ledger (mirrors
--    stripe_webhook_events); Svix redelivers, so the handler must dedup by svix-id.
--
-- Internal objects: RLS on + the Supabase default-ACL grant revoked (only the
-- service-role outbox + webhook touch them).

begin;

set local lock_timeout = '5s';

create table if not exists notification_suppression (
  channel       text not null check (channel in ('email', 'telegram')),
  address       text not null,
  reason        text,
  source        text,  -- 'bounce' | 'complaint' | 'unsubscribe' | 'manual'
  suppressed_at timestamptz not null default now(),
  primary key (channel, address)
);
alter table notification_suppression enable row level security;
revoke all on notification_suppression from anon, authenticated;

alter table channel_sends drop constraint if exists channel_sends_status_check;
alter table channel_sends add constraint channel_sends_status_check
  check (status in ('queued','sent','failed','delivered','bounced','complained','suppressed'));

create table if not exists resend_webhook_events (
  event_id    text primary key,   -- the svix-id delivery header
  type        text,
  received_at timestamptz not null default now(),
  payload     jsonb
);
alter table resend_webhook_events enable row level security;
revoke all on resend_webhook_events from anon, authenticated;

commit;
