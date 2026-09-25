-- 560: one merge, one undo (AUTODEDUP production sprint W3, decisions 3 and 8).
--
-- 1. `property_merge_events_listing_live_idx`: `detach_listing` moves ONE advert back to the
--    `prev_property_id` of its oldest LIVE ledger row (`listing_ref_id`, migration 323); the
--    ledger was only ever read by group, so nothing indexed the advert.
-- 2. The ONE-TIME COPY: the operator's merges made before rulings existed (source 'operator',
--    at least one row live; the W0 probe counted 362 groups) become "same" rulings, so the
--    engine can never undo them. Pair grain: two adverts that sat on DIFFERENT properties of the
--    group — an advert's side is its origin, so one another merge brought there joins no pair —
--    and still share one property now. `decided_by = 'operator'` (the ledger kept no login),
--    dated when the operator merged; a pair already ruled or operator-vetoed is left alone.
--
-- ADDITIVE: one partial index and INSERTs into `autodedup.verdicts`, nothing updated or removed.
-- Idempotent: `if not exists`, the existing-ruling guard and `on conflict do nothing`.

set lock_timeout = '5s';

create index if not exists property_merge_events_listing_live_idx
  on property_merge_events (listing_ref_id, id) where undone_at is null;

with live as (
  select e.listing_ref_id, e.prev_property_id, e.id
  from property_merge_events e
  where e.undone_at is null and e.listing_ref_id is not null
),
origin as (
  select distinct on (v.listing_ref_id) v.listing_ref_id as listing_id, v.prev_property_id as side
  from live v
  order by v.listing_ref_id, v.id
),
operator_groups as (
  select e.merge_group_id, min(e.created_at) as merged_at
  from property_merge_events e
  where e.source = 'operator'
  group by e.merge_group_id
  having bool_or(e.undone_at is null)
),
group_properties as (
  select distinct e.merge_group_id, p.property_id
  from property_merge_events e
  join operator_groups g on g.merge_group_id = e.merge_group_id
  cross join lateral (values (e.survivor_property_id), (e.retired_property_id)) p(property_id)
),
members as (
  select gp.merge_group_id, o.listing_id, o.side
  from group_properties gp
  join origin o on o.side = gp.property_id
  union
  select gp.merge_group_id, l.id, l.property_id
  from group_properties gp
  join listings l on l.property_id = gp.property_id
  where not exists (select 1 from live v where v.listing_ref_id = l.id)
),
placed as (
  select m.merge_group_id, m.listing_id, m.side, l.property_id as now_on
  from members m
  join listings l on l.id = m.listing_id
),
pairs as (
  select distinct on (a.listing_id, b.listing_id)
         a.listing_id as lo, b.listing_id as hi, a.merge_group_id, g.merged_at
  from placed a
  join placed b
    on b.merge_group_id = a.merge_group_id
   and b.listing_id > a.listing_id
   and b.side <> a.side
   and b.now_on = a.now_on
  join operator_groups g on g.merge_group_id = a.merge_group_id
  order by a.listing_id, b.listing_id, g.merged_at
)
insert into autodedup.verdicts (kind, listing_lo, listing_hi, verdict, note, decided_by, decided_at)
select 'pair', p.lo, p.hi, 'same',
       'operator merge ' || p.merge_group_id::text || ' (copied by migration 560)',
       'operator', p.merged_at
from pairs p
where not exists (
        select 1 from autodedup.verdicts v
        where v.kind = 'pair' and v.listing_lo = p.lo and v.listing_hi = p.hi)
  and not exists (
        select 1 from autodedup.must_not_link n
        where n.listing_lo = p.lo and n.listing_hi = p.hi and n.source = 'operator')
on conflict (kind, listing_lo, listing_hi, decided_by) where kind = 'pair' do nothing;
