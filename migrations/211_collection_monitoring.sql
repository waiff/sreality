-- 211: per-collection monitoring config — opt a collection into change alerts.
--
-- Sprint C (unified notifications): a collection can be MONITORED — its member
-- properties' price/lifecycle changes raise notifications (the collection-monitor
-- producer, written against the shared notifications contract in
-- docs/design/notifications-unified.md). Monitoring is per-collection (the
-- operator toggles it), so it is a flag on the collection, not a separate table.
-- `notify_channels` is the source's channel choice the producer folds into
-- notifications.target_channels (notifications-unified.md §6 decision 4); empty =
-- in-app only (in-app needs no channel row). `is_system` protects the seeded
-- default "monitoring" collection from rename/delete.
--
-- Additive: three not-null-with-default columns + one idempotent seed row + a
-- view refresh that only APPENDS columns (existing ones unchanged).

alter table collections
  add column if not exists monitoring_enabled boolean not null default false,
  add column if not exists notify_channels    text[]  not null default '{}',
  add column if not exists is_system          boolean not null default false;

-- The default monitoring collection: ships with monitoring ON, protected from
-- rename/delete. Idempotent on lower(name) so a re-apply never duplicates it.
insert into collections (name, description, monitoring_enabled, is_system)
select 'monitoring',
       'Default collection — properties added here raise change alerts (price moves, delisting).',
       true, true
where not exists (select 1 from collections where lower(name) = 'monitoring');

-- expose the new columns on the anon read view (append-only; existing columns
-- and their order are unchanged, so CREATE OR REPLACE is safe).
create or replace view collections_public as
  select c.id, c.name, c.description, c.created_at, c.updated_at,
         (select count(*) from collection_properties cp
          where cp.collection_id = c.id) as listing_count,
         c.monitoring_enabled, c.notify_channels, c.is_system
  from collections c;
