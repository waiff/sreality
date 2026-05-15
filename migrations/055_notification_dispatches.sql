-- 055_notification_dispatches.sql
--
-- Phase U2.7: Append-only audit + dedup guard for notification matches.
--
-- One row per (subscription, listing) match. The UNIQUE constraint is
-- the dedup primitive: the matcher can re-run safely on overlapping
-- windows because ON CONFLICT DO NOTHING preserves the first dispatch.
--
-- `channel` and `status` are CHECK-bounded, not enums, so adding a new
-- delivery channel (Telegram, email, push) is a one-line ALTER. Today
-- only the in-app feed page is wired up.
--
-- `seen_at` is the operator's read marker for the feed UI. Null means
-- unread; the frontend sets it on click.
--
-- ROADMAP line 1075 notes the future Dedup track (D1) will rename the
-- dedup key from sreality_id to a canonical listing_id once D1 ships.
-- That is a single-column rename; no functional change to the matcher.

begin;

create table notification_dispatches (
    id               uuid        primary key default gen_random_uuid(),
    subscription_id  uuid        not null
        references notification_subscriptions(id) on delete cascade,
    sreality_id      bigint      not null,
    dispatched_at    timestamptz not null default now(),
    channel          text        not null default 'in_app'
        check (channel in ('in_app')),
    status           text        not null default 'sent'
        check (status in ('sent', 'failed')),
    error_message    text,
    seen_at          timestamptz,
    unique (subscription_id, sreality_id)
);

create index notification_dispatches_dispatched_at_idx
    on notification_dispatches (dispatched_at desc);

create index notification_dispatches_unread_idx
    on notification_dispatches (subscription_id, seen_at)
    where seen_at is null;

alter table notification_dispatches enable row level security;

comment on table notification_dispatches is
    'Append-only audit + dedup guard for notification matches '
    '(Phase U2.7). The UNIQUE (subscription_id, sreality_id) constraint '
    'guarantees a given (subscription, listing) pair fires at most one '
    'notification across every matcher run.';

comment on column notification_dispatches.channel is
    'Delivery channel. CHECK-bounded, not an enum, so growing the set '
    '(telegram, email, push) is a one-line ALTER. v1 ships in_app only.';

comment on column notification_dispatches.seen_at is
    'Read marker for the /notifications feed UI. Null means unread; the '
    'frontend sets it on row click via POST /notifications/dispatches/{id}/mark-seen.';

commit;
