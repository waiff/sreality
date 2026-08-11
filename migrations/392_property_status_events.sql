-- 392_property_status_events.sql
-- Durable, append-only log of `properties.is_active` transitions (property
-- grain — the "does this property currently have >=1 active source listing"
-- aggregate that `_cheap_property_rollup`/the incremental+daily property-
-- maintenance sweep already maintain, rule #20). Piggybacks on that EXISTING
-- aggregate rather than re-deriving "any active source" from raw listing
-- events client-side, so Browse/badges/this log can never disagree
-- (CLAUDE.md "app-wide unification").
--
-- Motivating gap: `listings.inactive_at` is cleared back to NULL on every
-- reactivation (`touch_listings*`, scraper/db.py), so today only the single
-- MOST RECENT transition is knowable for any listing, and nothing at all was
-- recorded at property grain. The Listing Detail price-history chart
-- (frontend/src/lib/priceHistory.ts) needs real gaps for periods a property
-- had zero active listings — this table is the source of truth it reads.
--
-- One-time seed below is best-effort: an 'active' event at first_seen_at for
-- every property, plus an 'inactive' event at last_seen_at (the latest
-- confirmed-active moment — properties carries no inactive_at column) for
-- properties currently inactive. Earlier mid-history relist gaps, if any
-- happened before this migration, are unrecoverable: the source data never
-- retained them. Every future transition is captured going forward via the
-- trigger below, at every write site, present and future, with no per-site
-- instrumentation to maintain.
--
-- Already APPLIED to production under this shape (via MCP, verified with a
-- SELECT: 953,599 seeded rows). The initial apply granted the view to `anon`
-- following the pattern on listings_public/property_sources_public — those
-- are grandfathered pre-migration-299 exceptions, not the current posture
-- (CI's test_anon_holds_no_relation_grants caught the drift); corrected to
-- `authenticated` here and re-applied to production to match. This file just
-- gives it its permanent tracked number — a concurrent branch claimed 390,
-- then 391, locally while this was in flight, hence 392.

create table property_status_events (
  id          bigserial primary key,
  property_id bigint not null references properties(id) on delete cascade,
  is_active   boolean not null,
  event_at    timestamptz not null,
  created_at  timestamptz not null default now()
);

create index property_status_events_property_id_event_at_idx
  on property_status_events (property_id, event_at);

alter table property_status_events enable row level security;
-- No anon/authenticated policy, same deny-all posture as listings/
-- listing_snapshots/properties — reads go through the _public view below.

insert into property_status_events (property_id, is_active, event_at)
select id, true, first_seen_at from properties;

insert into property_status_events (property_id, is_active, event_at)
select id, false, last_seen_at
from properties
where is_active = false;

create function log_property_status_event() returns trigger
language plpgsql
as $$
begin
  if TG_OP = 'INSERT' then
    insert into property_status_events (property_id, is_active, event_at)
    values (NEW.id, NEW.is_active, now());
  elsif OLD.is_active is distinct from NEW.is_active then
    insert into property_status_events (property_id, is_active, event_at)
    values (NEW.id, NEW.is_active, now());
  end if;
  return NEW;
end;
$$;

-- Not SECURITY DEFINER and not callable as an ordinary RPC (a trigger-return
-- function errors if invoked directly), but revoke explicitly anyway to match
-- this project's "revoke on every new function" default-privilege posture.
revoke execute on function log_property_status_event() from public;

-- `OF is_active` scopes the trigger to statements that actually touch the
-- column (mirrors the admin-geo trigger's `OF geom` scoping) — the IS
-- DISTINCT FROM guard inside the function still gates on a REAL flip, since
-- most such statements re-affirm the same value (e.g. every dirty-property
-- rollup of an already-active property).
create trigger properties_log_status_event
  after insert or update of is_active on properties
  for each row
  execute function log_property_status_event();

revoke all on property_status_events from anon, authenticated;

create view property_status_events_public as
select property_id, is_active, event_at
from property_status_events;

-- anon holds NO relation grants (migration 299's settled Phase 0 posture — the
-- SPA is fully login-gated and reads as authenticated; CI's
-- test_anon_holds_no_relation_grants enforces this on every new view). This is
-- a brand-new view, so unlike the grandfathered pre-299 anon grants on
-- listings_public/property_sources_public it follows the current posture from
-- day one.
grant select on property_status_events_public to authenticated;
