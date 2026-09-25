"""`probes set=readiness_v1`: the fixed, read-only production probe of the AUTODEDUP go-live sprint (W0).

IMMUTABLE once shipped: a changed query is `readiness_v2`, so two artifacts of one set compare.
D7 carve-out (PLAN W0): these reads of `property_merge_events` measure legacy state and size its
undo; nothing read here ever becomes engine input. Deleted in W8 with the legacy retirement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SET_NAME = "readiness_v1"

# Spelled exactly as `export` takes blocks; the SQL's trial predicate is the union of the
# export's own per-grain membership (`obec_kod = code` / `cast_obce_kod = code`).
TRIAL_BLOCKS: tuple[str, ...] = ("town:563510", "town:577626", "quarter:490245")


@dataclass(frozen=True)
class ReadinessQuery:
    name: str
    sql: str
    why: str
    drives: str


R0_PRECONDITIONS_SQL = """
select 'setting' as kind, s.key as name, s.value::text as value
  from public.app_settings s
 where s.key in ('autodedup_apply_enabled', 'autodedup_apply_scope', 'realtime_autodedup_enabled')
union all
select 'relation', r.name, coalesce(to_regclass(r.name)::text, 'ABSENT')
  from (values ('autodedup.applied_merges'), ('autodedup.unapplied_generations')) as r(name)
union all
select 'constraint', c.conname, pg_get_constraintdef(c.oid)
  from pg_constraint c
 where c.conrelid = 'public.property_merge_events'::regclass and c.contype = 'c'
"""

R1_MERGE_GROUPS_SQL = """
with trial as (
  select ll.listing_id from public.listing_location ll
   where ll.obec_kod in (563510, 577626) or ll.cast_obce_kod = 490245
),
ev as (
  select e.merge_group_id, e.source, e.generation, e.created_at, e.undone_at,
         e.retired_property_id, coalesce(e.listing_ref_id, l.id) as listing_id
    from public.property_merge_events e
    left join public.listings l on e.listing_ref_id is null and l.sreality_id = e.listing_id
),
grp as (
  select ev.merge_group_id,
         min(ev.source) as source,
         min(coalesce(ev.generation, 'none')) as generation,
         case when bool_and(ev.undone_at is null) then 'live'
              when bool_and(ev.undone_at is not null) then 'undone' else 'partial' end as state,
         case when bool_and(t.listing_id is not null) then 'inside'
              when bool_or(t.listing_id is not null) then 'straddles' else 'outside' end as trial_area,
         count(*) as events,
         count(distinct ev.retired_property_id) as retired,
         min(ev.created_at) as merged_at
    from ev left join trial t on t.listing_id = ev.listing_id
   group by ev.merge_group_id
)
select source, generation, state, trial_area, count(*) as groups, sum(events) as listing_events,
       sum(retired) as retired_properties, min(merged_at) as first_at, max(merged_at) as last_at
  from grp
 group by source, generation, state, trial_area
 order by source, generation, state, trial_area
"""

R1B_REASONS_BY_SOURCE_SQL = """
select e.source, e.reason,
       count(distinct e.merge_group_id) as groups,
       count(distinct e.merge_group_id) filter (where e.undone_at is null) as live_groups
  from public.property_merge_events e
 group by e.source, e.reason
 order by e.source, groups desc
"""

R1C_LIVE_GROUP_HEALTH_SQL = """
with trial as (
  select ll.listing_id from public.listing_location ll
   where ll.obec_kod in (563510, 577626) or ll.cast_obce_kod = 490245
),
live as (
  select e.merge_group_id, e.source, e.created_at, e.survivor_property_id,
         coalesce(e.listing_ref_id, l.id) as listing_id
    from public.property_merge_events e
    left join public.listings l on e.listing_ref_id is null and l.sreality_id = e.listing_id
   where e.undone_at is null
),
later_operator as (
  select e.survivor_property_id as property_id, max(e.created_at) as last_at
    from public.property_merge_events e
   where e.source = 'operator' and e.undone_at is null
   group by e.survivor_property_id
),
grp as (
  select lv.merge_group_id, min(lv.source) as source, min(lv.created_at) as merged_at,
         max(lv.survivor_property_id) as survivor,
         bool_or(t.listing_id is not null) as touches_trial,
         bool_or(lv.listing_id is null) as listing_gone,
         bool_or(cur.property_id is distinct from lv.survivor_property_id) as child_off_survivor,
         count(*) as events
    from live lv
    left join public.listings cur on cur.id = lv.listing_id
    left join trial t on t.listing_id = lv.listing_id
   group by lv.merge_group_id
)
select g.source, g.touches_trial,
       case when s.status <> 'active' then 'survivor_merged_on'
            when g.listing_gone then 'listing_gone'
            when g.child_off_survivor then 'children_moved'
            when o.last_at > g.merged_at then 'operator_built_on'
            else 'intact' end as health,
       count(*) as groups, sum(g.events) as events
  from grp g
  join public.properties s on s.id = g.survivor
  left join later_operator o on o.property_id = g.survivor
 group by 1, 2, 3
 order by 1, 2, 3
"""

R2_MERGED_AWAY_LEDGER_SQL = """
with last_ev as (
  select distinct on (e.retired_property_id)
         e.retired_property_id, e.source, coalesce(e.generation, 'none') as generation,
         (e.undone_at is null) as live
    from public.property_merge_events e
   order by e.retired_property_id, e.created_at desc, e.id desc
)
select p.status, coalesce(le.source, 'no_event') as merged_by,
       coalesce(le.generation, 'none') as generation,
       coalesce(le.live, false) as event_live,
       coalesce(q.status = 'merged_away', false) as into_merged_away,
       count(*) as properties
  from public.properties p
  left join last_ev le on le.retired_property_id = p.id
  left join public.properties q on q.id = p.merged_into
 where p.status = 'merged_away' or le.retired_property_id is not null
 group by 1, 2, 3, 4, 5
 order by 1, 2, 3, 4, 5
"""

R3_MULTI_LISTING_PROVENANCE_SQL = """
with trial as (
  select ll.listing_id from public.listing_location ll
   where ll.obec_kod in (563510, 577626) or ll.cast_obce_kod = 490245
),
multi as (
  select l.property_id from public.listings l
   where l.property_id is not null
   group by l.property_id having count(*) > 1
),
arrival as (
  select distinct on (coalesce(e.listing_ref_id, l2.id), e.survivor_property_id)
         coalesce(e.listing_ref_id, l2.id) as listing_id, e.survivor_property_id as property_id, e.source
    from public.property_merge_events e
    left join public.listings l2 on e.listing_ref_id is null and l2.sreality_id = e.listing_id
   where e.undone_at is null
   order by coalesce(e.listing_ref_id, l2.id), e.survivor_property_id, e.created_at desc, e.id desc
),
kid as (
  select l.property_id, l.category_type, coalesce(a.source, 'native') as arrived_by,
         (t.listing_id is not null) as in_trial
    from public.listings l
    join multi m on m.property_id = l.property_id
    left join arrival a on a.listing_id = l.id and a.property_id = l.property_id
    left join trial t on t.listing_id = l.id
),
prop as (
  select k.property_id, count(*) as listings,
         count(*) filter (where k.arrived_by = 'native') as native,
         bool_or(k.arrived_by = 'auto') as has_auto,
         bool_or(k.arrived_by = 'operator') as has_operator,
         bool_or(k.arrived_by = 'autodedup') as has_autodedup,
         bool_or(k.in_trial) as in_trial,
         count(distinct k.category_type) > 1 as mixed_deal_type
    from kid k group by k.property_id
)
select p.status, pr.in_trial,
       concat_ws('+', case when pr.has_auto then 'auto' end,
                      case when pr.has_operator then 'operator' end,
                      case when pr.has_autodedup then 'autodedup' end,
                      case when pr.native >= 2 then 'native_multi' end) as provenance,
       count(*) as properties, sum(pr.listings) as listings, max(pr.listings) as max_listings,
       count(*) filter (where pr.mixed_deal_type) as mixed_deal_type
  from prop pr join public.properties p on p.id = pr.property_id
 group by 1, 2, 3
 order by 1, 2, 3
"""

R4_ASSET_LINKS_SQL = """
with trial as (
  select ll.listing_id from public.listing_location ll
   where ll.obec_kod in (563510, 577626) or ll.cast_obce_kod = 490245
),
linked as (
  select p.id, p.asset_id, p.status,
         exists (select 1 from public.listings l join trial t on t.listing_id = l.id
                  where l.property_id = p.id) as in_trial
    from public.properties p where p.asset_id is not null
)
select 'properties' as kind, k.status as a, k.in_trial::text as b, count(*) as n,
       count(distinct k.asset_id) as assets
  from linked k group by 2, 3
union all
select 'membership_events', e.source, e.action, count(*), count(distinct e.asset_id)
  from public.asset_membership_events e group by 2, 3
union all
select 'assets', a.status, null, count(*), count(*) from public.assets a group by 2
order by 1, 2, 3
"""

R5_UNATTACHED_LISTINGS_SQL = """
with trial as (
  select ll.listing_id from public.listing_location ll
   where ll.obec_kod in (563510, 577626) or ll.cast_obce_kod = 490245
)
select l.source, l.is_active, count(*) as listings,
       count(*) filter (where t.listing_id is not null) as in_trial,
       count(*) filter (where l.first_seen_at < now() - interval '15 minutes') as older_15m,
       count(*) filter (where l.first_seen_at < now() - interval '1 day') as older_1d,
       min(l.first_seen_at) as oldest, max(l.first_seen_at) as newest
  from public.listings l left join trial t on t.listing_id = l.id
 where l.property_id is null
 group by 1, 2 order by 3 desc
"""

R6_OPERATOR_STATE_ON_SURVIVORS_SQL = """
with trial as (
  select ll.listing_id from public.listing_location ll
   where ll.obec_kod in (563510, 577626) or ll.cast_obce_kod = 490245
),
surv as (
  select x.property_id, x.source,
         exists (select 1 from public.listings l join trial t on t.listing_id = l.id
                  where l.property_id = x.property_id) as in_trial
    from (select distinct e.survivor_property_id as property_id, e.source
            from public.property_merge_events e where e.undone_at is null) x
    join public.properties p on p.id = x.property_id and p.status = 'active'
),
moved_back as (
  select distinct e.survivor_property_id as property_id, e.source,
         coalesce(e.listing_ref_id, l2.id) as listing_id
    from public.property_merge_events e
    left join public.listings l2 on e.listing_ref_id is null and l2.sreality_id = e.listing_id
   where e.undone_at is null
)
select s.source, s.in_trial, 'survivors' as state_table, count(*) as n_rows,
       count(distinct s.property_id) as properties from surv s group by 1, 2
union all
select s.source, s.in_trial, 'collection_properties', count(*), count(distinct s.property_id)
  from surv s join public.collection_properties x on x.property_id = s.property_id group by 1, 2
union all
select s.source, s.in_trial, 'property_tags', count(*), count(distinct s.property_id)
  from surv s join public.property_tags x on x.property_id = s.property_id group by 1, 2
union all
select s.source, s.in_trial, 'property_notes', count(*), count(distinct s.property_id)
  from surv s join public.property_notes x on x.property_id = s.property_id group by 1, 2
union all
select s.source, s.in_trial, 'property_notes_about_a_listing_undo_moves_away', count(*),
       count(distinct s.property_id)
  from surv s
  join public.property_notes x on x.property_id = s.property_id
  join moved_back mb on mb.property_id = s.property_id and mb.source = s.source
                    and mb.listing_id = x.origin_listing_ref_id
 group by 1, 2
union all
select s.source, s.in_trial, 'property_pipeline', count(*), count(distinct s.property_id)
  from surv s join public.property_pipeline x on x.property_id = s.property_id group by 1, 2
union all
select s.source, s.in_trial, 'property_dismissals_active', count(*), count(distinct s.property_id)
  from surv s join public.property_dismissals x on x.property_id = s.property_id
 where x.lifted_at is null group by 1, 2
union all
select s.source, s.in_trial, 'property_dismissals_lifted_by_merge', count(*), count(distinct s.property_id)
  from surv s join public.property_dismissals x on x.property_id = s.property_id
 where x.lift_reason = 'merge' group by 1, 2
union all
select s.source, s.in_trial, 'notification_dispatches', count(*), count(distinct s.property_id)
  from surv s join public.notification_dispatches x on x.property_id = s.property_id group by 1, 2
union all
select s.source, s.in_trial, 'property_status_events', count(*), count(distinct s.property_id)
  from surv s join public.property_status_events x on x.property_id = s.property_id group by 1, 2
order by 1, 2, 3
"""

R7_TRIAL_EXPECTED_MERGES_SQL = """
with trial as (
  select ll.listing_id from public.listing_location ll
   where ll.obec_kod in (563510, 577626) or ll.cast_obce_kod = 490245
),
m as (
  select cm.generation, cm.cluster_key, cm.listing_id, l.property_id, l.category_type,
         (t.listing_id is not null) as in_trial
    from autodedup.cluster_members cm
    left join public.listings l on l.id = cm.listing_id
    left join trial t on t.listing_id = cm.listing_id
   where left(cm.generation, 2) <> 'rt'
),
g as (
  select m.generation, m.cluster_key, count(*) as members,
         count(distinct m.property_id) as properties,
         bool_or(m.property_id is null) as unattached,
         count(distinct m.category_type) as deal_types, min(m.category_type) as deal_type
    from m group by m.generation, m.cluster_key
  having bool_or(m.in_trial)
)
select g.generation, c.status, g.deal_type, (c.block_grain is null) as cross_block,
       count(*) as groups,
       count(*) filter (where g.properties >= 2) as would_merge,
       coalesce(sum(g.properties - 1) filter (where g.properties >= 2), 0) as properties_to_retire,
       count(*) filter (where g.properties = 1 and not g.unattached) as already_one_property,
       count(*) filter (where g.unattached) as unattached_member,
       count(*) filter (where g.deal_types > 1) as deal_type_mix,
       count(*) filter (where g.members > 8) as oversize_members
  from g join autodedup.clusters c on c.generation = g.generation and c.cluster_key = g.cluster_key
 group by 1, 2, 3, 4
 order by 1, 2, 3, 4
"""

R7B_TRIAL_GROUPS_VS_MERGES_SQL = """
with trial as (
  select ll.listing_id from public.listing_location ll
   where ll.obec_kod in (563510, 577626) or ll.cast_obce_kod = 490245
),
arrival as (
  select distinct on (coalesce(e.listing_ref_id, l2.id), e.survivor_property_id)
         coalesce(e.listing_ref_id, l2.id) as listing_id, e.survivor_property_id as property_id, e.source
    from public.property_merge_events e
    left join public.listings l2 on e.listing_ref_id is null and l2.sreality_id = e.listing_id
   where e.undone_at is null
   order by coalesce(e.listing_ref_id, l2.id), e.survivor_property_id, e.created_at desc, e.id desc
),
m as (
  select cm.generation, cm.cluster_key, l.property_id, (t.listing_id is not null) as in_trial
    from autodedup.cluster_members cm
    left join public.listings l on l.id = cm.listing_id
    left join trial t on t.listing_id = cm.listing_id
   where left(cm.generation, 2) <> 'rt'
),
tg as (select m.generation, m.cluster_key from m group by 1, 2 having bool_or(m.in_trial)),
inv as (
  select distinct m.generation, m.cluster_key, m.property_id
    from m join tg on tg.generation = m.generation and tg.cluster_key = m.cluster_key
   where m.property_id is not null
),
kid as (
  select i.generation, i.cluster_key, i.property_id, l.id as listing_id,
         coalesce(a.source, 'native') as arrived_by, o.cluster_key as kid_group
    from inv i
    join public.listings l on l.property_id = i.property_id
    left join arrival a on a.listing_id = l.id and a.property_id = l.property_id
    left join autodedup.cluster_members o on o.generation = i.generation and o.listing_id = l.id
),
grp as (
  select k.generation, k.cluster_key,
         count(distinct k.property_id) as properties,
         count(distinct k.listing_id) as listings_after_merge,
         count(*) filter (where k.kid_group is null) as ungrouped,
         count(*) filter (where k.kid_group <> k.cluster_key) as in_other_group,
         string_agg(distinct k.arrived_by, '+' order by k.arrived_by)
           filter (where k.arrived_by <> 'native') as merged_by
    from kid k group by k.generation, k.cluster_key
)
select generation,
       case when properties >= 2 then 'would_merge' else 'already_one_property' end as shape,
       case when ungrouped > 0 and in_other_group > 0 then 'ungrouped+spans'
            when ungrouped > 0 then 'carries_ungrouped'
            when in_other_group > 0 then 'spans_groups' else 'clean' end as foreign_listings,
       coalesce(merged_by, 'none') as properties_hold_merges_by,
       count(*) as groups,
       count(*) filter (where listings_after_merge > 8) as oversize_after_merge
  from grp
 group by 1, 2, 3, 4
 order by 1, 2, 3, 4
"""

NEG_CONTRADICTED_NEGATIVES_SQL = """
with arrival as (
  select distinct on (coalesce(e.listing_ref_id, l2.id), e.survivor_property_id)
         coalesce(e.listing_ref_id, l2.id) as listing_id, e.survivor_property_id as property_id, e.source
    from public.property_merge_events e
    left join public.listings l2 on e.listing_ref_id is null and l2.sreality_id = e.listing_id
   where e.undone_at is null
   order by coalesce(e.listing_ref_id, l2.id), e.survivor_property_id, e.created_at desc, e.id desc
),
neg as (
  select v.listing_lo, v.listing_hi, 'verdict_' || v.verdict as kind
    from autodedup.verdicts v
   where v.kind = 'pair'
     and v.verdict in ('different', 'same_building_different_unit', 'same_project_different_unit')
  union
  select n.listing_lo, n.listing_hi, 'must_not_link_' || n.source from autodedup.must_not_link n
),
hit as (
  select neg.kind, a.property_id, a.id as lo, b.id as hi
    from neg
    join public.listings a on a.id = neg.listing_lo
    join public.listings b on b.id = neg.listing_hi
   where a.property_id = b.property_id
),
attributed as (
  select h.kind, h.property_id,
         coalesce((select string_agg(distinct ar.source, '+' order by ar.source)
                     from arrival ar
                    where ar.property_id = h.property_id and ar.listing_id in (h.lo, h.hi)), 'native') as joined_by
    from hit h
)
select kind, joined_by, count(*) as pairs, count(distinct property_id) as properties
  from attributed group by 1, 2 order by 1, 2
"""

R8_AUTODEDUP_RELATION_SIZES_SQL = """
select c.relname as relation, c.reltuples::bigint as est_rows,
       pg_total_relation_size(c.oid) as total_bytes, pg_relation_size(c.oid) as heap_bytes,
       pg_indexes_size(c.oid) as index_bytes
  from pg_class c join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'autodedup' and c.relkind in ('r', 'p')
 order by total_bytes desc
"""

R8B_STORE_ROWS_BY_GENERATION_SQL = """
select 'pairs' as relation, p.generation, p.zone as bucket, count(*) as n_rows,
       sum(pg_column_size(p.*))::bigint as row_bytes
  from autodedup.pairs p group by p.generation, p.zone
union all
select 'clusters', c.generation, c.status, count(*), sum(pg_column_size(c.*))::bigint
  from autodedup.clusters c group by c.generation, c.status
union all
select 'cluster_members', m.generation, null, count(*), sum(pg_column_size(m.*))::bigint
  from autodedup.cluster_members m group by m.generation
order by 1, 2, 3
"""

R9_DISPATCHES_A_MERGE_TOUCHES_SQL = """
with trial as (
  select ll.listing_id from public.listing_location ll
   where ll.obec_kod in (563510, 577626) or ll.cast_obce_kod = 490245
),
m as (
  select cm.generation, cm.cluster_key, l.property_id, (t.listing_id is not null) as in_trial
    from autodedup.cluster_members cm
    left join public.listings l on l.id = cm.listing_id
    left join trial t on t.listing_id = cm.listing_id
   where left(cm.generation, 2) <> 'rt'
),
tg as (
  select m.generation, m.cluster_key from m group by 1, 2
  having bool_or(m.in_trial) and count(distinct m.property_id) >= 2
),
inv as (
  select distinct m.generation, m.cluster_key, m.property_id
    from m join tg on tg.generation = m.generation and tg.cluster_key = m.cluster_key
   where m.property_id is not null
)
select i.generation, d.source_kind, d.change_kind,
       count(*) as rows_on_involved_properties,
       count(*) - count(distinct (i.cluster_key, coalesce(d.subscription_id::text, '-'),
                                  coalesce(d.collection_id::text, '-'), d.change_kind,
                                  coalesce(d.trigger_snapshot_id::text, '-'))) as rows_a_merge_deletes,
       count(distinct i.property_id) as properties
  from inv i join public.notification_dispatches d on d.property_id = i.property_id
 group by 1, 2, 3
 order by 1, 2, 3
"""

A_FIELD_DISAGREEMENT_SQL = """
with prop as (
  select l.property_id, count(*) as active_listings,
         count(distinct l.price_czk) as prices, min(l.price_czk) as price_min, max(l.price_czk) as price_max,
         count(distinct l.area_m2) as areas, min(l.area_m2) as area_min, max(l.area_m2) as area_max,
         count(distinct l.disposition) as dispositions,
         count(distinct l.floor) as floors
    from public.listings l
   where l.is_active and l.property_id is not null
   group by l.property_id
  having count(*) > 1
)
select p.category_type, count(*) as properties, sum(pr.active_listings) as active_listings,
       count(*) filter (where pr.prices > 1) as price_differs,
       count(*) filter (where pr.price_max > pr.price_min * 1.05) as price_differs_over_5pct,
       count(*) filter (where pr.areas > 1) as area_differs,
       count(*) filter (where pr.area_max > pr.area_min * 1.05) as area_differs_over_5pct,
       count(*) filter (where pr.dispositions > 1) as disposition_differs,
       count(*) filter (where pr.floors > 1) as floor_differs,
       count(*) filter (where pr.prices > 1 or pr.areas > 1 or pr.dispositions > 1
                          or pr.floors > 1) as any_differs
  from prop pr join public.properties p on p.id = pr.property_id and p.status = 'active'
 group by 1
 order by 2 desc
"""

A_ENGINE_MERGE_DISAGREEMENT_LIST_SQL = """
with merged as (
  select distinct on (a.survivor_property_id)
         a.survivor_property_id as property_id, a.generation, a.applied_at as merged_at
    from autodedup.applied_merges a
   where a.outcome = 'applied' and a.undone_at is null
   order by a.survivor_property_id, a.applied_at desc, a.id desc
),
prop as (
  select l.property_id, count(*) as n_active_adverts,
         min(l.area_m2) as area_min, max(l.area_m2) as area_max,
         count(distinct l.disposition) as n_dispositions,
         count(distinct l.floor) as n_floors,
         array_agg(distinct l.floor order by l.floor) filter (where l.floor is not null) as floors,
         array_agg(distinct l.area_m2 order by l.area_m2)
           filter (where l.area_m2 is not null) as areas,
         array_agg(distinct l.disposition order by l.disposition)
           filter (where l.disposition is not null) as dispositions,
         array_agg(distinct l.source order by l.source) as sources,
         array_agg(l.id order by l.id) as listing_ids
    from public.listings l
    join merged m on m.property_id = l.property_id
   where l.is_active and l.property_id is not null
   group by l.property_id
  having count(*) > 1
)
select m.property_id, m.generation, m.merged_at, pr.n_active_adverts,
       pr.floors, pr.areas, pr.dispositions, pr.sources, pr.listing_ids,
       pr.n_floors > 1 as floor_differs,
       coalesce(pr.area_max > pr.area_min * 1.05, false) as area_differs_over_5pct,
       pr.n_dispositions > 1 as dispo_differs
  from merged m
  join prop pr on pr.property_id = m.property_id
  join public.properties p on p.id = m.property_id and p.status = 'active'
 where pr.n_floors > 1 or pr.area_max > pr.area_min * 1.05 or pr.n_dispositions > 1
 order by m.merged_at desc, m.property_id desc
 limit 200
"""

A_CANONICAL_ORDER_TIES_SQL = """
with multi as (
  select l.property_id from public.listings l
   where l.property_id is not null
   group by l.property_id having count(*) > 1
),
ranked as (
  select l.property_id, l.id, l.source, l.sreality_id,
         rank() over (partition by l.property_id
                      order by l.is_active desc, public.source_trust_rank(l.source),
                               l.last_seen_at desc nulls last) as pos
    from public.listings l
    join multi m on m.property_id = l.property_id
),
top as (
  select r.property_id, min(p.status) as status, count(*) as tied,
         count(distinct r.source) as tied_portals,
         count(*) filter (where r.sreality_id is null) as tied_without_sreality_id,
         coalesce(bool_or(r.id = p.repr_listing_ref_id), false) as repr_in_top
    from ranked r join public.properties p on p.id = r.property_id
   where r.pos = 1
   group by r.property_id
)
select t.status, count(*) as multi_listing_properties,
       count(*) filter (where t.tied >= 2) as tied_at_top,
       count(*) filter (where t.tied >= 2 and t.tied_portals = 1) as tied_same_portal,
       count(*) filter (where t.tied_without_sreality_id >= 2) as tie_todays_order_leaves_open,
       count(*) filter (where not t.repr_in_top) as repr_outside_canonical_top
  from top t
 group by 1
 order by 1
"""

A_STATUS_EVENTS_ON_MERGED_AWAY_SQL = """
select count(*) as events, count(distinct e.property_id) as properties,
       count(*) filter (where e.created_at >= p.merged_at) as written_at_or_after_merge,
       count(*) filter (where e.created_at >= p.merged_at and not e.is_active) as inactive_after_merge,
       max(e.created_at) as newest
  from public.property_status_events e
  join public.properties p on p.id = e.property_id
 where p.status = 'merged_away'
"""

A_FALSE_ALERT_RATE_SQL = """
with touched as (
  select distinct c.property_id
    from public.listing_snapshots s
    join public.listings c on c.id = s.listing_id
   where s.scraped_at > now() - interval '7 days' and c.property_id is not null
),
steps as (
  select c.property_id, s.listing_id, s.scraped_at, s.price_czk,
         lag(s.price_czk) over w as prev,
         lag(s.listing_id) over w as prev_listing_id
    from public.listing_snapshots s
    join public.listings c on c.id = s.listing_id
    join touched t on t.property_id = c.property_id
   where s.price_czk is not null
  window w as (partition by c.property_id order by s.scraped_at, s.id)
)
select case when st.price_czk < st.prev then 'drop' else 'rise' end as direction,
       count(*) as steps_7d,
       count(*) filter (where st.prev_listing_id <> st.listing_id) as cross_advert_steps_7d,
       count(*) filter (where st.scraped_at > now() - interval '2 days') as steps_2d,
       count(*) filter (where st.scraped_at > now() - interval '2 days'
                          and st.prev_listing_id <> st.listing_id) as cross_advert_steps_2d,
       count(distinct st.property_id) as properties,
       count(distinct st.property_id) filter (where st.prev_listing_id <> st.listing_id)
         as properties_with_cross_advert_steps
  from steps st
 where st.prev is not null and st.price_czk <> st.prev
   and st.scraped_at > now() - interval '7 days'
 group by 1
 order by 1
"""

E_ARRIVAL_TO_EVIDENCE_SQL = """
with fresh as (
  select l.id, l.source, l.first_seen_at
    from public.listings l
   where l.first_seen_at > now() - interval '7 days'
),
per as (
  select f.id, f.source, f.first_seen_at,
         count(i.id) as images,
         count(i.id) filter (where i.storage_path is not null and i.phash is null) as stored_unhashed,
         min(i.last_download_attempt_at) filter (where i.phash is not null) as first_phash_at,
         min(i.clip_tagged_at) as first_clip_at
    from fresh f
    left join public.images i on i.listing_id = f.id
   group by f.id, f.source, f.first_seen_at
)
select coalesce(p.source, 'ALL') as source,
       count(*) as listings,
       count(*) filter (where p.images > 0) as with_images,
       count(p.first_phash_at) as with_phash,
       count(p.first_clip_at) as with_clip,
       sum(p.stored_unhashed) as stored_unhashed_images,
       percentile_cont(0.5) within group
         (order by extract(epoch from p.first_phash_at - p.first_seen_at)::float8) as phash_p50_s,
       percentile_cont(0.9) within group
         (order by extract(epoch from p.first_phash_at - p.first_seen_at)::float8) as phash_p90_s,
       percentile_cont(0.5) within group
         (order by extract(epoch from p.first_clip_at - p.first_seen_at)::float8) as clip_p50_s,
       percentile_cont(0.9) within group
         (order by extract(epoch from p.first_clip_at - p.first_seen_at)::float8) as clip_p90_s
  from per p
 group by grouping sets ((p.source), ())
 order by 1
"""

E_AUTODEDUP_SETTINGS_SQL = """
select s.key, s.value, s.updated_at, s.updated_by
  from autodedup.settings s
 order by s.key
"""

E_UNUSED_AUTODEDUP_TABLES_SQL = """
select 'listing_fp' as relation, count(*) as n_rows from autodedup.listing_fp
union all select 'image_band', count(*) from autodedup.image_band
union all select 'exploded_blocks', count(*) from autodedup.exploded_blocks
union all select 'merges', count(*) from autodedup.merges
union all select 'labels', count(*) from autodedup.labels
union all select 'models', count(*) from autodedup.models
union all select 'eval_samples', count(*) from autodedup.eval_samples
union all select 'resolve_queue', count(*) from autodedup.resolve_queue
union all select 'judge_queue', count(*) from autodedup.judge_queue
"""

B_LEGACY_TABLE_SIZES_SQL = """
select s.schemaname, s.relname, s.n_live_tup, pg_total_relation_size(s.relid) as total_bytes,
       s.seq_scan, s.idx_scan, s.last_seq_scan, s.last_idx_scan
  from pg_stat_user_tables s
 where s.schemaname in ('public', 'dedup_sim')
   and s.relname ~ '(dedup|tag_|image_tag|dinov3|exam|training_examples|border|phash_pair|visual_matches|plan_matches|room_class|backup_464)'
 order by total_bytes desc
"""

B_PUBLISHED_AT_RESIDUE_SQL = """
select count(*) as properties,
       count(*) filter (where p.published_at is null) as unpublished,
       count(*) filter (where p.publish_reason is not null) as with_publish_reason,
       pg_relation_size(to_regclass('public.properties_unpublished_idx')) as unpublished_idx_bytes,
       pg_relation_size(to_regclass('public.properties_published_at_idx')) as published_at_idx_bytes,
       (select i.idx_scan from pg_stat_user_indexes i
         where i.indexrelid = to_regclass('public.properties_unpublished_idx')) as unpublished_idx_scans,
       (select i.idx_scan from pg_stat_user_indexes i
         where i.indexrelid = to_regclass('public.properties_published_at_idx')) as published_at_idx_scans
  from public.properties p
"""

BC_APP_SETTINGS_LEFTOVERS_SQL = """
select s.key, left(s.value::text, 200) as value, length(s.value::text) as value_chars, s.updated_at
  from public.app_settings s
 where s.key ~ '^(autodedup|realtime_autodedup|realtime_images)'
    or s.key ~ '(dedup|label|tag_|dino|exam|publication|visual_match|room_classify)'
 order by s.key
"""

B_LEGACY_CRON_JOBS_SQL = """
select j.jobid, j.jobname, j.schedule, j.active, left(j.command, 200) as command
  from cron.job j
 where j.jobname ~ '(dedup|tag|label|exam)' or j.command ~ '(dedup|tag_|label|exam|dinov3)'
 order by j.jobname
"""

B_HAND_LABELS_BY_SOURCE_SQL = """
select l.source, l.created_by, l.state, count(*) as cells,
       count(distinct l.image_id) as images, count(distinct l.tag_id) as tags,
       count(*) filter (where l.verified_at is not null) as verified,
       min(l.created_at) as first_at, max(l.updated_at) as last_at
  from public.image_tag_labels l
 group by 1, 2, 3
 order by 1, 2, 3
"""

B_TAG_HEAD_MODELS_SQL = """
select m.id, m.version, m.label, m.status, m.created_at, m.activated_at, m.mode, m.model,
       m.revision, cardinality(m.heads) as heads,
       (select count(*) from public.image_tag_scores s where s.model_id = m.id) as scored_images,
       (select max(s.scored_at) from public.image_tag_scores s where s.model_id = m.id) as last_scored_at
  from public.tag_head_models m
 order by m.id
"""


READINESS_V1: tuple[ReadinessQuery, ...] = (
    ReadinessQuery(
        "r0_preconditions", R0_PRECONDITIONS_SQL,
        "Is migration 558 applied, and are the apply/realtime switches where the plan thinks?",
        "W1/W2 preconditions: an ABSENT applied_merges means the apply adapter cannot run; the "
        "property_merge_events source CHECK must admit 'autodedup'.",
    ),
    ReadinessQuery(
        "r1_merge_groups", R1_MERGE_GROUPS_SQL,
        "Merge groups by source x generation stamp x live/undone x trial area; source='auto' is "
        "the legacy engine, generation='legacy' is NOT (migration 475 stamped every 09-05 row).",
        "Decision 3: how many legacy groups must be undone inside the trial area before the "
        "engine enters; an 'auto' row with generation 'none' is a writer leaning on the DEFAULT.",
    ),
    ReadinessQuery(
        "r1b_reasons_by_source", R1B_REASONS_BY_SOURCE_SQL,
        "Merge reasons per source: did pre-cutoff 'operator' rows include queue confirmations?",
        "Decision 3: whether every source='operator' merge is a genuine operator ruling to keep.",
    ),
    ReadinessQuery(
        "r1c_live_group_health", R1C_LIVE_GROUP_HEALTH_SQL,
        "What unmerge_group would actually do to each LIVE group: intact, children moved, "
        "listing gone, survivor merged on, or operator built on.",
        "Decision 3 undo rule: only 'intact' auto groups inside the area are undone, newest "
        "first; zero intact auto groups in the trial area means that step is never built.",
    ),
    ReadinessQuery(
        "r2_merged_away_ledger", R2_MERGED_AWAY_LEDGER_SQL,
        "merged_away properties by who retired them, plus ledger integrity (retired with no "
        "standing event, reactivated without an undo, chains into a retired row).",
        "W1/W3: whether the ledger can drive a deterministic undo; property_not_active "
        "refusals counted in advance.",
    ),
    ReadinessQuery(
        "r3_multi_listing_provenance", R3_MULTI_LISTING_PROVENANCE_SQL,
        "Properties with more than one listing today and how the extra listings arrived "
        "(auto / operator / autodedup / native ingest-time grouping).",
        "Decision 3 ingest-time groupings (native_multi has no ledger) and the "
        "category_type_mix refusal (mixed_deal_type is legacy damage).",
    ),
    ReadinessQuery(
        "r4_asset_links", R4_ASSET_LINKS_SQL,
        "Asset links on properties, asset membership events by source/action, assets by status.",
        "Decision 17: a group spanning two different asset links is refused; sizes "
        "asset_linked_units in advance and shows whether any 'auto' asset link exists.",
    ),
    ReadinessQuery(
        "r5_unattached_listings", R5_UNATTACHED_LISTINGS_SQL,
        "Listings with property_id NULL; maintenance attaches every 5 min, so older_15m > 0 "
        "is a stuck attach.",
        "unattached_member refusals: one stuck attach refuses its whole group (W2 gate).",
    ),
    ReadinessQuery(
        "r6_operator_state_on_survivors", R6_OPERATOR_STATE_ON_SURVIVORS_SQL,
        "Operator state riding on live merge survivors by source and trial area: collections, "
        "tags, notes, pipeline, dismissals, dispatches, status events.",
        "Decision 3 undo cost: what an undo leaves on the survivor (best-effort carry, "
        "set-table collisions deleted at merge time, dismissals lifted by merge).",
    ),
    ReadinessQuery(
        "r7_trial_expected_merges", R7_TRIAL_EXPECTED_MERGES_SQL,
        "The trial area's expected merges per non-rt generation: would_merge, properties to "
        "retire, unattached members, deal-type mix, oversize, cross-block groups.",
        "W2 trial sizing, and the silent out_of_scope loss: cross_block groups carry no block "
        "and a blocks scope never admits them.",
    ),
    ReadinessQuery(
        "r7b_trial_groups_vs_merges", R7B_TRIAL_GROUPS_VS_MERGES_SQL,
        "How many trial groups touch an already merged property, and whose merge it was.",
        "Decision 3: engine groups REFUSED because of a legacy merge (carries_ungrouped / "
        "spans_groups + auto) against legacy over-merges silently accepted.",
    ),
    ReadinessQuery(
        "neg_contradicted_negatives", NEG_CONTRADICTED_NEGATIVES_SQL,
        "Production properties already contradicting a pair-grain negative ruling (verdict or "
        "must_not_link), attributed to the merge that joined the pair.",
        "Decision 8: the engine obeys 'different' rulings forever; sizes the production "
        "over-merges already ruled against, by who made them.",
    ),
    ReadinessQuery(
        "r8_autodedup_relation_sizes", R8_AUTODEDUP_RELATION_SIZES_SQL,
        "Size of every autodedup relation: estimated rows, heap and index bytes.",
        "Decision 7 / W5 storage: the growth budget before the worker lane is the one path.",
    ),
    ReadinessQuery(
        "r8b_store_rows_by_generation", R8B_STORE_ROWS_BY_GENERATION_SQL,
        "Rows and bytes per generation and zone/status in pairs, clusters and cluster_members "
        "(E: pair rows by generation and zone).",
        "Decision 7: reject pairs are not stored; reject-zone row_bytes is what dropping them "
        "saves; old generations are pruned on promotion.",
    ),
    ReadinessQuery(
        "r9_dispatches_a_merge_touches", R9_DISPATCHES_A_MERGE_TOUCHES_SQL,
        "notification_dispatches a trial merge would re-point, and how many the carry would "
        "DELETE on a key collision.",
        "Decision 16: the notification defects W1 fixes before the first live merge; the rows "
        "a trial merge deletes.",
    ),
    ReadinessQuery(
        "a_field_disagreement", A_FIELD_DISAGREEMENT_SQL,
        "Active multi-listing properties whose active adverts disagree on price, area, "
        "disposition or floor, per field (a NULL against a value is not counted).",
        "Decisions 11 and 18: how often the canonical advert decides what the property card "
        "shows.",
    ),
    ReadinessQuery(
        "a_engine_merge_disagreement_list", A_ENGINE_MERGE_DISAGREEMENT_LIST_SQL,
        "ROWS, not counts: active properties surviving a live engine merge (applied_merges "
        "outcome 'applied', not undone) whose active adverts disagree on floor, area beyond 5 "
        "percent or disposition (both stated, a_field_disagreement's own predicates), newest "
        "merge first, at most 200.",
        "Decision 18 / trial week: the operator's review list of engine merges whose adverts "
        "disagree on a stated field",
    ),
    ReadinessQuery(
        "a_canonical_order_ties", A_CANONICAL_ORDER_TIES_SQL,
        "Multi-listing properties whose top place under active-first, trust rank, most "
        "recently seen is shared (only the id decides), ties today's sreality_id order leaves "
        "open, and stored reprs outside that top set.",
        "Decision 18: the id tiebreak of the canonical order and the display churn the one "
        "rule causes.",
    ),
    ReadinessQuery(
        "a_status_events_on_merged_away", A_STATUS_EVENTS_ON_MERGED_AWAY_SQL,
        "property_status_events rows on merged_away properties, and how many were written at "
        "or after the merge (the 392 trigger firing on the retired row).",
        "Decision 16: the fake status event written on merge (W1 fix, and whether a data "
        "cleanup is owed).",
    ),
    ReadinessQuery(
        "a_false_alert_rate", A_FALSE_ALERT_RATE_SQL,
        "Property-grain price steps in the last 7 days (and 2, the watchdog default) and how "
        "many compare adjacent snapshots of DIFFERENT adverts.",
        "Decision 16: price alerts mixing the adverts of one property; the false-alert rate "
        "every merge multiplies.",
    ),
    ReadinessQuery(
        "e_arrival_to_evidence", E_ARRIVAL_TO_EVIDENCE_SQL,
        "Listings first seen in the last 7 days: image/pHash/CLIP coverage and p50/p90 "
        "seconds to the first pHash (proxy: the hashed image's last download attempt, pHash "
        "runs at download) and to the first CLIP tag.",
        "Decision 10 / E F3: the photo hold waits for complete evidence; the latency floor of "
        "every photo-resting merge in the worker lane (W5).",
    ),
    ReadinessQuery(
        "e_autodedup_settings", E_AUTODEDUP_SETTINGS_SQL,
        "Every autodedup.settings row, including the lane's rate rows (rt_pass_rate_per_s:*) "
        "and bootstrap flags (rt_bootstrap:*).",
        "W5 gate (worker rate at least 2x corpus inflow) and W7 (one settings file): the rows "
        "left to delete.",
    ),
    ReadinessQuery(
        "e_unused_autodedup_tables", E_UNUSED_AUTODEDUP_TABLES_SQL,
        "Row counts of the nine migration-528 tables no shipped code reads or writes.",
        "W8: dropping them is a destructive migration; zero rows confirms nothing is lost.",
    ),
    ReadinessQuery(
        "b_legacy_table_sizes", B_LEGACY_TABLE_SIZES_SQL,
        "Legacy dedup and L2 tables (public + dedup_sim): live rows, total bytes, seq/index "
        "scans and when each was last read.",
        "Decision 4 / W8: what the legacy deletion drops, what the R2 backup must hold, and "
        "whether anything still reads a table marked DELETE.",
    ),
    ReadinessQuery(
        "b_published_at_residue", B_PUBLISHED_AT_RESIDUE_SQL,
        "properties.published_at / publish_reason residue: the unpublished count and the two "
        "published_at indexes' size and scans.",
        "W8: drop the unread publication-gate columns and indexes.",
    ),
    ReadinessQuery(
        "bc_app_settings_leftovers", BC_APP_SETTINGS_LEFTOVERS_SQL,
        "app_settings rows of the apply/realtime switches and of the legacy dedup and "
        "labeling programs (value cut to 200 characters).",
        "Decision 6 + W7/W8: the switches to delete (only the scope row survives the trial; "
        "realtime keeps only its interval).",
    ),
    ReadinessQuery(
        "b_legacy_cron_jobs", B_LEGACY_CRON_JOBS_SQL,
        "pg_cron jobs whose name or command names dedup, tag, label or exam work.",
        "W8: the cron jobs unscheduled with the legacy deletion.",
    ),
    ReadinessQuery(
        "b_hand_labels_by_source", B_HAND_LABELS_BY_SOURCE_SQL,
        "image_tag_labels cells by who made them (source, created_by) and state.",
        "Decision 4: the human labour KEPT (labeling page, tag-head training) and what the R2 "
        "dump must preserve.",
    ),
    ReadinessQuery(
        "b_tag_head_models", B_TAG_HEAD_MODELS_SQL,
        "tag_head_models versions, their status, and how many images each scored in "
        "image_tag_scores.",
        "Decision 10: the engine reads the ACTIVE tag-head model's winners; is one active and "
        "what does it cover?",
    ),
)


def _rows(probes: dict[str, Any], name: str) -> list[dict[str, Any]] | None:
    """A query's rows, or None when it failed (a failed query never lands under `probes`)."""
    rows = probes.get(name)
    return rows if isinstance(rows, list) else None


def _total(rows: list[dict[str, Any]] | None, column: str, **match: Any) -> int | None:
    """Sum `column` over the rows matching every `match` pair; None when the query failed."""
    if rows is None:
        return None
    return sum(
        int(row.get(column) or 0)
        for row in rows
        if all(row.get(key) == value for key, value in match.items())
    )


def headline(probes: dict[str, Any]) -> dict[str, Any]:
    """The numbers each go-live decision turns on; None = that query failed."""
    r1 = _rows(probes, "r1_merge_groups")
    r1c = _rows(probes, "r1c_live_group_health")
    r3 = _rows(probes, "r3_multi_listing_provenance")
    r7 = _rows(probes, "r7_trial_expected_merges")
    alerts = _rows(probes, "a_false_alert_rate")
    steps, cross = _total(alerts, "steps_7d"), _total(alerts, "cross_advert_steps_7d")
    evidence = next(
        (r for r in _rows(probes, "e_arrival_to_evidence") or [] if r.get("source") == "ALL"), {}
    )
    relations = _rows(probes, "r0_preconditions")
    cron = _rows(probes, "b_legacy_cron_jobs")
    return {
        "apply_relations_absent": None if relations is None else [
            r.get("name") for r in relations
            if r.get("kind") == "relation" and r.get("value") == "ABSENT"
        ],
        "legacy_live_groups_inside_trial":
            _total(r1, "groups", source="auto", state="live", trial_area="inside"),
        "legacy_live_groups_straddling_trial":
            _total(r1, "groups", source="auto", state="live", trial_area="straddles"),
        "legacy_intact_groups_touching_trial":
            _total(r1c, "groups", source="auto", touches_trial=True, health="intact"),
        "native_multi_properties": None if r3 is None else _total(
            [r for r in r3 if "native_multi" in str(r.get("provenance") or "")], "properties"
        ),
        "mixed_deal_type_properties": _total(r3, "mixed_deal_type"),
        "unattached_older_15m": _total(_rows(probes, "r5_unattached_listings"), "older_15m"),
        "trial_would_merge_by_generation": None if r7 is None else {
            gen: _total(r7, "would_merge", generation=gen)
            for gen in sorted({str(r.get("generation")) for r in r7})
        },
        "trial_cross_block_groups": _total(r7, "groups", cross_block=True),
        "negatives_contradicted_pairs":
            _total(_rows(probes, "neg_contradicted_negatives"), "pairs"),
        "dispatches_a_trial_merge_deletes":
            _total(_rows(probes, "r9_dispatches_a_merge_touches"), "rows_a_merge_deletes"),
        "status_events_on_merged_away":
            _total(_rows(probes, "a_status_events_on_merged_away"), "events"),
        "false_alert_share_7d": round(cross / steps, 4) if steps and cross is not None else None,
        "clip_p50_s": evidence.get("clip_p50_s"),
        "clip_p90_s": evidence.get("clip_p90_s"),
        "legacy_cron_jobs": None if cron is None else len(cron),
        "active_tag_head_model": next(
            (r.get("version") for r in _rows(probes, "b_tag_head_models") or []
             if r.get("status") == "active"),
            None,
        ),
    }
