-- 054_notification_subscriptions.sql
--
-- Phase U2.7: New-listing notifications.
--
-- One row per saved-search subscription. `filter_spec` is the canonical
-- Browse filter dict (the same shape `toolkit/comparables._shared_filter_where`
-- consumes), stored as JSONB so the schema doesn't have to track every
-- filter facet the UI might add. The dispatch worker reads this row,
-- builds SQL via `_shared_filter_where`, and finds matching new listings.
--
-- Per Phase U2.7's open questions, today's identity model is one shared
-- operator (no per-user accounts), so there is no `user_id` column. When
-- multi-recipient notifications open up that's a single ALTER ADD COLUMN.
--
-- No RLS policies: writes flow through the bearer-gated FastAPI service
-- (service-role connection); the browser never writes directly. Reads
-- also go through the API today.

begin;

create table notification_subscriptions (
    id           uuid        primary key default gen_random_uuid(),
    name         text        not null,
    filter_spec  jsonb       not null,
    is_active    boolean     not null default true,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create index notification_subscriptions_active_idx
    on notification_subscriptions (is_active) where is_active;

alter table notification_subscriptions enable row level security;

create or replace function notification_subscriptions_touch_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

create trigger notification_subscriptions_touch_updated_at_trg
    before update on notification_subscriptions
    for each row execute function notification_subscriptions_touch_updated_at();

comment on table notification_subscriptions is
    'Saved-search subscriptions for new-listing notifications (Phase U2.7). '
    'Each row is one named filter spec; the dispatch worker matches new '
    'listings against the spec and writes one notification_dispatches row '
    'per match.';

comment on column notification_subscriptions.filter_spec is
    'Canonical Browse filter dict (same shape as toolkit/comparables.'
    '_shared_filter_where). Stored as JSONB so new filter facets do not '
    'require schema migrations.';

comment on column notification_subscriptions.is_active is
    'Soft-delete and pause flag. Inactive subscriptions are skipped by the '
    'dispatch worker but kept around so their history in '
    'notification_dispatches is readable.';

commit;
