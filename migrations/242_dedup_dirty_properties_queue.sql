-- 242_dedup_dirty_properties_queue.sql
-- Wave 4c: real-time per-listing dedup. The dedup engine's full scan runs every 6h and
-- the candidate drain every 2h, so a NEW cross-portal listing shows as its own property in
-- Browse for up to hours before it merges into the existing group (and the watchdog can
-- fire a false "new property" alert in that window). This work queue closes that gap:
-- when a listing becomes dedup-ready (its images get CLIP-tagged — pHash runs just before),
-- its property is enqueued here, and the dirty drain (dedup_engine --dirty) re-decides ONLY
-- the street groups touching a dirty property via the SAME resolve_pair, in minutes.
--
-- Identical shape + race-free drain discipline to dirty_properties (migration 106, rule #20):
-- writers append (ON CONFLICT DO UPDATE SET marked_at), the drain claims rows dirtied at/
-- before a cutoff and deletes only those untouched since, so a mid-run re-dirty survives. The
-- daily full scan is the reconcile backstop.
create table dedup_dirty_properties (
  property_id bigint primary key references properties(id) on delete cascade,
  marked_at   timestamptz not null default now()
);

-- Internal work queue; never read by the browser. No anon policy => RLS denies anon by
-- default (same posture as dirty_properties / listing_detail_queue).
alter table dedup_dirty_properties enable row level security;

create index dedup_dirty_properties_marked_at_idx on dedup_dirty_properties (marked_at);

comment on table dedup_dirty_properties is
  'Wave 4c work queue: property ids whose listings just became dedup-ready (CLIP-tagged). '
  'Writers append; dedup_engine --dirty re-decides only the street groups touching them via '
  'resolve_pair, then deletes the drained rows. The 6h full scan is the reconcile backstop.';
