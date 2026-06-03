-- 151_filter_presets.sql
--
-- Saved filter presets for the Browse page.
-- (Applied to production via Supabase MCP under the bookkeeping name
-- `150_filter_presets`, before a concurrent session claimed 150; renumbered
-- to 151 on disk to keep the migrations/ sequence collision-free. The live
-- table matches the DDL below exactly.)
--
-- One row per named filter set. `filter_spec` is the *native* Browse
-- `ListingFilters` object (camelCase frontend shape), stored verbatim as
-- JSONB and treated as an opaque blob by the API — a preset is restored
-- entirely client-side and is never matched server-side, so the backend
-- never interprets the spec. This is deliberately decoupled from
-- `notification_subscriptions` (the Watchdog table): a preset has no firing
-- behaviour, no cursor, and must round-trip the *full* filter set losslessly,
-- whereas the watchdog spec is a curated subset consumed by the matcher.
-- Storing the native shape means new Browse filters are captured for free,
-- with no schema or Python-model churn.
--
-- Single-operator identity model (no `user_id`), same as
-- notification_subscriptions; multi-recipient is a future ALTER ADD COLUMN.
--
-- No RLS policies: writes flow through the bearer-gated FastAPI service
-- (service-role connection); the browser never writes directly.

begin;

create table filter_presets (
    id           uuid        primary key default gen_random_uuid(),
    name         text        not null,
    filter_spec  jsonb       not null,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

alter table filter_presets enable row level security;

create or replace function filter_presets_touch_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

create trigger filter_presets_touch_updated_at_trg
    before update on filter_presets
    for each row execute function filter_presets_touch_updated_at();

comment on table filter_presets is
    'Named, reusable Browse filter presets. One row per preset; restored '
    'client-side only (never matched server-side). Decoupled from '
    'notification_subscriptions on purpose.';

comment on column filter_presets.filter_spec is
    'Native Browse ListingFilters object (camelCase), stored verbatim as an '
    'opaque JSONB blob. The API does not interpret it.';

commit;
