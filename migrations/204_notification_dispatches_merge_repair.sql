-- 204: one-time repair — re-point notification_dispatches rows orphaned onto
-- merged_away properties.
--
-- Watchdog dispatches are property-anchored (FK property_id -> properties), but
-- merge_properties historically re-pointed only listings, never dispatches, so
-- rows on a merged property were left pointing at the merged_away loser and
-- vanished from the feed (which reads the active property grain). Going forward
-- the toolkit.operator_state reconciler re-points them inline on every merge;
-- this migration cleans up the rows orphaned before that existed.
--
-- Re-point each orphan to its TERMINAL active survivor (chasing
-- properties.merged_into through chained merges), collapsing on the dedup key
-- (subscription_id, property_id, change_kind). Idempotent: touches only rows on
-- a merged_away property, so it is a no-op on a fresh DB or on re-run.

-- (a) drop orphans that would collide with a dispatch already on the survivor
with recursive survivor as (
  select id as orig_id, merged_into as next_id
  from properties where status = 'merged_away'
  union all
  select s.orig_id, p.merged_into
  from survivor s join properties p on p.id = s.next_id
  where p.status = 'merged_away'
),
terminal as (
  select s.orig_id, s.next_id as survivor_id
  from survivor s join properties p on p.id = s.next_id
  where p.status = 'active'
)
delete from notification_dispatches r
using terminal t
where r.property_id = t.orig_id
  and exists (
    select 1 from notification_dispatches s
    where s.property_id = t.survivor_id
      and s.subscription_id = r.subscription_id
      and s.change_kind = r.change_kind
  );

-- (b) re-point the rest onto the terminal survivor
with recursive survivor as (
  select id as orig_id, merged_into as next_id
  from properties where status = 'merged_away'
  union all
  select s.orig_id, p.merged_into
  from survivor s join properties p on p.id = s.next_id
  where p.status = 'merged_away'
),
terminal as (
  select s.orig_id, s.next_id as survivor_id
  from survivor s join properties p on p.id = s.next_id
  where p.status = 'active'
)
update notification_dispatches r
set property_id = t.survivor_id
from terminal t
where r.property_id = t.orig_id;
