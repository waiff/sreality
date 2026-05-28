-- 106_dirty_properties_queue.sql
--
-- Phase 3 of the scaling roadmap: real-time properties via dirty-set incremental
-- recompute. Today scripts/recompute_property_stats.py recomputes EVERY property
-- every 30 min (O(all properties); price-history window functions over every
-- snapshot). New listings written by the detail-drain (property_id NULL, Tier-1
-- deferred per Phase 2) only gain a `properties` parent on the next full run, so
-- Browse lags by up to a cron interval, and the full recompute will not scale to
-- 5-10 portals.
--
-- This migration is the data-layer foundation: a small work queue of property
-- ids whose child listings changed since the last maintenance pass. The writers
-- (the detail-drain's write_detail_batch, mark_inactive, mark_listing_inactive,
-- and touch_listings' reactivation subset) append the affected property_id with a
-- cheap set-based INSERT ... ON CONFLICT DO NOTHING. The incremental property-
-- maintenance job (recompute_property_stats --incremental, cron */5) drains the
-- queue and recomputes ONLY those properties with the existing batch SQL scoped
-- to id = ANY(...). New listings (property_id NULL) are NOT queued here — they are
-- resolved by the job's straggler-attach phase (Tier-1 matcher + singleton insert),
-- which already scans property_id IS NULL rows.
--
-- The daily FULL sweep (recompute_property_stats with no --incremental flag)
-- stays the reconcile backstop: it recomputes every property and clears this
-- queue, so a missed enqueue self-heals within 24h. Tier-2 fuzzy dedup
-- (scripts/dedup_sweep.py) is unchanged.
--
-- Purely additive: new table only. No existing object is altered.

create table dirty_properties (
  property_id bigint primary key references properties(id) on delete cascade,
  marked_at   timestamptz not null default now()
);

-- Internal work queue; never read by the browser. No anon policy => RLS denies
-- anon by default (same posture as listing_detail_queue, migration 105).
alter table dirty_properties enable row level security;

comment on table dirty_properties is
  'Phase 3 work queue: property ids whose child listings changed since the last '
  'incremental property-maintenance pass. Writers append; '
  'recompute_property_stats --incremental drains + recomputes scoped, then deletes. '
  'The daily full sweep clears it as a backstop.';
