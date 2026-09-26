-- 564_autodedup_operator_merges.sql
--
-- AUTODEDUP E299 / D87: the operator's own Browse merges become ENGINE DATA, inside schema
-- `autodedup`. Migration 563 was taken by the sibling trial-scope-rentals branch; this is the
-- next free number. Purely ADDITIVE.
--
-- The operator ruled on 2026-09-25: "use the 362 groups to make our thing better (even pull
-- these decisions to your schema, so that you do not have to work with legacy structures)".
--
-- WHY THE ENGINE KEEPS ITS OWN COPY (D7). The engine reads nothing from any prior dedup effort
-- (PROGRAM.md section 0, "Clean slate is total"): not `property_merge_events`, not as labels,
-- not as evaluation truth. Migration 560 already copied the operator's merges into
-- `autodedup.verdicts`, but only as anonymous pair rulings — a note naming the group is all
-- that tied a pair back to the merge it came from, and the group itself (who was merged with
-- whom, from which side, when) existed only in the production ledger. Using the groups as a
-- yardstick or as labels would have meant reading that ledger at evaluation time, every time,
-- through whatever shape it has that day. So the groups are copied ONCE, here, and from now on
-- the engine, its lanes and its harness read `autodedup.operator_merges` and never the ledger:
-- no legacy structure is read at decision or evaluation time. Only the operator's OWN merges
-- (`source = 'operator'`) are copied — the removed engine's merges (`'auto'`) stay unread
-- (CLAUDE.md rule 15), and the engine's own (`'autodedup'`) are its ledger's business (558).
--
-- 1. `autodedup.operator_merges`: one row per operator merge group with at least one live
--    ledger row (the W0 probe counted 362; 560 copied the same set). `member_ids` are the
--    group's adverts (listings.id, ascending) and `member_sides[i]` the ORIGIN property of
--    `member_ids[i]` — the property it sat on before its oldest live move, or its own property
--    when no live move ever touched it — exactly 560's derivation: an advert another merge
--    brought onto a group property joins no pair of this group. `member_property_ids[i]` is
--    where the advert sat when this copy ran, so the pairs the group asserts are reproducible
--    from the row alone: two members on DIFFERENT sides that still shared one property
--    (`n_pairs`). `source` is 'browse' (the one place the operator merges today); `status` is
--    'live' at copy time and exists for later corrections ('undone' when the operator takes
--    the merge apart, 'withdrawn' when they retract it as a label), with `status_note` /
--    `status_at` saying why and when. Nothing updates it yet.
--
-- 2. `autodedup.verdicts.operator_merge_group_id` (nullable, no default: a metadata-only
--    ALTER): the pair-grain link from a 'same' ruling to the merge it was ruled by, so a reader
--    tells a Browse merge from a verdict typed against the pair with a COLUMN rather than by
--    parsing the note. Backfilled here for the two note shapes the code has ever written for a
--    merge — 560's copy ('operator merge <group> (copied by migration 560)') and the operator
--    merge path since 559 (`toolkit.property_identity.merge_property_set`: 'operator merge
--    <group>'). This is the one and last time a note is parsed. No foreign key: a verdict row
--    must never fail on the group row's absence.
--
-- ONE-TIME COPY, read-only towards `public`: the CTEs below are 560's, verbatim up to the
-- pairs, then aggregated per group. Idempotent: `if not exists`, `on conflict do nothing`, and
-- the backfill touches only rows whose link is still NULL — a re-run adds nothing.
--
-- POSTURE. Backend-only like every autodedup relation: RLS on, anon/authenticated revoked, no
-- `_public` view; registered in tests/test_migration_rls_grants.py and
-- tests/test_tenant_isolation_live.py::_ADMIN_ONLY_RELATIONS. No foreign key into production
-- (528's droppable-schema rule).

set lock_timeout = '5s';

------------------------------------------------------------------
-- 1. the engine's own record of the operator's merges
------------------------------------------------------------------

create table if not exists autodedup.operator_merges (
  merge_group_id        uuid        primary key,
  merged_at             timestamptz not null,
  survivor_property_id  bigint,
  retired_property_ids  bigint[]    not null default '{}',
  member_ids            bigint[]    not null default '{}',
  member_sides          bigint[]    not null default '{}',
  member_property_ids   bigint[]    not null default '{}',
  n_pairs               integer     not null default 0,
  events                integer     not null default 0,
  undone_events         integer     not null default 0,
  decided_by            text        not null default 'operator',
  source                text        not null default 'browse'
    check (source in ('browse')),
  status                text        not null default 'live'
    check (status in ('live', 'undone', 'withdrawn')),
  status_note           text,
  status_at             timestamptz,
  copied_at             timestamptz not null default now(),
  copied_by             text        not null default 'migration 564',
  constraint autodedup_operator_merges_members_ck check (
    cardinality(member_ids) = cardinality(member_sides)
    and cardinality(member_ids) = cardinality(member_property_ids)
  )
);

create index if not exists autodedup_operator_merges_members_idx
  on autodedup.operator_merges using gin (member_ids);

comment on table autodedup.operator_merges is
  'The operator''s own Browse merges (property_merge_events.source = ''operator''), copied ONCE '
  'by migration 564 so the engine never reads the production ledger (D7, E299). One row per '
  'merge group; member_sides[i] is the ORIGIN property of member_ids[i]; a pair is two members '
  'on different sides that shared one property at copy time.';

comment on column autodedup.operator_merges.member_sides is
  'Origin property of member_ids[i]: where it sat before its oldest live move, or its own '
  'property when no live move touched it (migration 560''s derivation).';

comment on column autodedup.operator_merges.member_property_ids is
  'Where member_ids[i] sat when the copy ran. Same side = never a pair; different sides on one '
  'property = a pair the operator asserted is one property.';

comment on column autodedup.operator_merges.status is
  '''live'' at copy time. For later corrections: ''undone'' (the operator took the merge apart), '
  '''withdrawn'' (retracted as a label). status_note and status_at say why and when.';

alter table autodedup.operator_merges enable row level security;
revoke all on autodedup.operator_merges from anon, authenticated;

------------------------------------------------------------------
-- 2. the pair-grain link: which merge a 'same' ruling came from
------------------------------------------------------------------

alter table autodedup.verdicts add column if not exists operator_merge_group_id uuid;

create index if not exists autodedup_verdicts_operator_merge_idx
  on autodedup.verdicts (operator_merge_group_id)
  where operator_merge_group_id is not null;

comment on column autodedup.verdicts.operator_merge_group_id is
  'The autodedup.operator_merges group this pair ruling was made by (a Browse merge), NULL for '
  'a verdict typed against the pair. Backfilled by migration 564; no foreign key.';

------------------------------------------------------------------
-- 3. the one-time copy (560's CTEs, aggregated per group)
------------------------------------------------------------------

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
  select e.merge_group_id,
         min(e.created_at) as merged_at,
         (array_agg(e.survivor_property_id order by e.id))[1] as survivor_property_id,
         array_agg(distinct e.retired_property_id order by e.retired_property_id)
           as retired_property_ids,
         count(*) as events,
         count(*) filter (where e.undone_at is not null) as undone_events
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
pair_counts as (
  select a.merge_group_id, count(*) as n_pairs
  from placed a
  join placed b
    on b.merge_group_id = a.merge_group_id
   and b.listing_id > a.listing_id
   and b.side <> a.side
   and b.now_on = a.now_on
  group by a.merge_group_id
),
member_sets as (
  select p.merge_group_id,
         array_agg(p.listing_id order by p.listing_id) as member_ids,
         array_agg(p.side order by p.listing_id) as member_sides,
         array_agg(p.now_on order by p.listing_id) as member_property_ids
  from placed p
  group by p.merge_group_id
)
insert into autodedup.operator_merges
  (merge_group_id, merged_at, survivor_property_id, retired_property_ids, member_ids,
   member_sides, member_property_ids, n_pairs, events, undone_events)
select g.merge_group_id, g.merged_at, g.survivor_property_id, g.retired_property_ids,
       coalesce(s.member_ids, '{}'::bigint[]), coalesce(s.member_sides, '{}'::bigint[]),
       coalesce(s.member_property_ids, '{}'::bigint[]), coalesce(c.n_pairs, 0)::integer,
       g.events::integer, g.undone_events::integer
from operator_groups g
left join member_sets s on s.merge_group_id = g.merge_group_id
left join pair_counts c on c.merge_group_id = g.merge_group_id
on conflict (merge_group_id) do nothing;

------------------------------------------------------------------
-- 4. the link, backfilled once from the two note shapes a merge has ever written
------------------------------------------------------------------

update autodedup.verdicts v
   set operator_merge_group_id = m.merge_group_id
  from autodedup.operator_merges m
 where v.kind = 'pair'
   and v.verdict = 'same'
   and v.operator_merge_group_id is null
   and v.note in ('operator merge ' || m.merge_group_id::text || ' (copied by migration 560)',
                  'operator merge ' || m.merge_group_id::text);

reset lock_timeout;
