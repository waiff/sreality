-- 207_channel_sends.sql
--
-- Sprint N PR 1: the channel-delivery ledger — the `llm_calls` of notification
-- delivery. One append-only row per send ATTEMPT to one external channel.
-- See docs/design/notification-channels.md.
--
-- Detection (notification_dispatches, the unified event table from migration 206)
-- stays SEPARATE from delivery: in-app delivery IS the event row (channel='in_app'),
-- while external channels (email/Telegram) record here. This is why a new channel
-- is NOT a `notification_dispatches.channel` widen — the dedup grain there can't
-- carry per-channel send state (status/retry/cost/provider id).
--
-- Two orthogonal idempotency keys:
--   detection: notification_dispatches.dedupe_key  (one event per sub/property/snapshot)
--   delivery:  channel_sends.dedupe_key            (one send per event x channel),
--              e.g. 'notif:{dispatch_uuid}:{channel}'. INSERT ... ON CONFLICT
--              (dedupe_key) DO NOTHING RETURNING id is the restart-safe claim.
--
-- This migration is purely ADDITIVE (new table) — it references existing tables
-- (notification_dispatches.id uuid, outreach_messages.id bigint) and is unused by
-- any running code until the Sprint N outbox + a transport land (ships dark).
--
-- Shared by THREE producers via the `consumer` discriminator: watchdog +
-- collection_monitor (origin = notification_dispatches) and outreach (origin =
-- outreach_messages). Exactly one origin FK is set, enforced by the CHECK. Both
-- FKs ON DELETE SET NULL so this spend/reliability ledger survives the origin row
-- being cleaned up (the denormalized source_kind/source_id keep attribution).

begin;

create table channel_sends (
  id                   bigserial primary key,
  created_at           timestamptz not null default now(),  -- = queued_at

  consumer             text not null
                         check (consumer in ('watchdog', 'collection_monitor', 'outreach')),
  notification_id      uuid   references notification_dispatches(id) on delete set null,
  outreach_message_id  bigint references outreach_messages(id)       on delete set null,
  -- denormalized telemetry: survives an origin delete + answers "noisiest source"
  -- without a deep join. source_id is text so it can hold a uuid (subscription_id)
  -- or a bigint (collection_id / campaign id) uniformly.
  source_kind          text,
  source_id            text,

  channel              text not null check (channel in ('email', 'telegram')),  -- widen by ALTER
  recipient            text not null,
  category             text not null default 'transactional'
                         check (category in ('transactional', 'commercial')),

  transport            text,                  -- vendor: 'resend' | 'telegram' | ...
  provider_message_id  text,
  status               text not null default 'queued'
                         check (status in ('queued', 'sent', 'failed')),  -- +delivered/bounced in the webhook PR
  error_message        text,
  attempts             int  not null default 0,
  next_attempt_at      timestamptz,           -- backoff cursor for the outbox retry pass
  cost_usd             numeric(10, 6),        -- NULL = unknown, 0 = known-free
  duration_ms          int,
  sent_at              timestamptz,

  dedupe_key           text not null,
  unique (dedupe_key),

  check (
    (consumer in ('watchdog', 'collection_monitor')
       and notification_id is not null and outreach_message_id is null)
 or (consumer = 'outreach'
       and outreach_message_id is not null and notification_id is null)
  )
);

create index channel_sends_created_idx on channel_sends (created_at desc);
create index channel_sends_retry_idx
  on channel_sends (status, next_attempt_at) where status in ('queued', 'failed');
create index channel_sends_notif_idx on channel_sends (notification_id);
create index channel_sends_outreach_idx on channel_sends (outreach_message_id);
create index channel_sends_source_idx on channel_sends (source_kind, source_id, created_at desc);

alter table channel_sends enable row level security;

comment on table channel_sends is
  'Append-only delivery ledger (the llm_calls of notification delivery). One row per '
  'send attempt to one external channel; in_app needs no row (the notification_dispatches '
  'event IS the in-app delivery). Shared by watchdog / collection_monitor / outreach via '
  'the consumer discriminator. See docs/design/notification-channels.md.';
comment on column channel_sends.dedupe_key is
  'Delivery idempotency key, one send per (event, channel) — e.g. notif:{dispatch_uuid}:{channel}. '
  'INSERT ... ON CONFLICT (dedupe_key) DO NOTHING RETURNING id is the restart-safe claim.';

commit;
