-- 482: portal_category_drift — the walk's "did the portal grow a category we do
-- not walk?" alarm, made queryable.
--
-- Part of the switch from absence-based to presence-verified delisting (rule #3
-- rewrite, 2026-09-07). With every closure now proven by fetching the listing's
-- own page, the failure that can still hurt is COVERAGE, not deletion: a portal
-- adds or renames a property type and our configuration keeps walking the old
-- list. mmreality's rentals were missing for four months exactly this way (mig
-- 481). So a portal that can read its own live category list (an optional
-- `live_categories` seam) compares it with the configured list before each walk
-- and records any difference here. Not a gate — with fetch verification a
-- missing category cannot cause a wrong deletion — an alarm: a log line expires
-- with the Actions run, a row does not.
--
-- Additive. RLS on with no policies: service-role writes bypass it, and nothing
-- reads it from the SPA (no anon/authenticated grant).
create table if not exists portal_category_drift (
  id            bigserial primary key,
  source        text        not null,
  observed_at   timestamptz not null default now(),
  live          jsonb       not null,   -- what the portal lists right now
  configured    jsonb       not null,   -- what portals.categories walks
  missing       jsonb       not null,   -- live but not configured (coverage gap)
  extra         jsonb       not null    -- configured but no longer live (dead walk)
);

alter table portal_category_drift enable row level security;

create index if not exists portal_category_drift_source_observed_idx
  on portal_category_drift (source, observed_at desc);

comment on table portal_category_drift is
  'Per-walk difference between a portal''s live category list and portals.categories. Alarm, not gate (rule #3, 2026-09-07).';
