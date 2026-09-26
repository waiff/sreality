"""Every statement the A1 apply path runs (PROGRAM.md E900-E906), as constants.

Reads: the `app_settings` scope row, one generation's groups and members from schema
`autodedup`, the member listings' `property_id`, categories and live location
(`listing_location`) and the involved properties from `public` (the facts the chokepoint itself
re-checks, plus where each advert is), the operator's negatives
(`verdicts`, `must_not_link`) and the engine's own apply ledger. NOTHING here reads or writes
`public.property_merge_events` (D7): only `toolkit.property_identity` writes it (merges, detaches
and the operator's native splits).

Every nullable parameter carries an explicit cast: psycopg sends no type OID for a Python
`None`, so an uncast NULL fails Parse with 42P18.
"""

from __future__ import annotations

# The rollout control, the scope row (migration 558 seeds it with no area: OFF).
# `public.app_settings`, not `autodedup.settings`: /settings edits the former, and the control
# that stops merges must be reachable there.
SETTING_SQL = """
select s.value
  from public.app_settings s
 where s.key = %(key)s::text
"""

# Every group of the generation. `status` is read, not filtered: a group that is not
# `proposed` is counted in the run summary rather than silently absent from it.
CLUSTERS_SQL = """
select c.cluster_key, c.size, c.status, c.min_edge_score, c.model_version, c.feature_version
  from autodedup.clusters c
 where c.generation = %(generation)s::text
 order by c.cluster_key
"""

# A member whose listing row is missing, or that has no property yet (rule 19: new rows land
# with a NULL property_id until maintenance attaches a singleton), comes back with NULLs. Where
# an advert IS is its live `listing_location` row (primary key listing_id): the scope's blocks
# are `town:` = obec_kod and `quarter:` = cast_obce_kod, the area legacy_retire.AREA_SQL reads
# (E904); an advert with no row comes back with both NULL and is inside no block.
MEMBERS_SQL = """
select m.cluster_key, m.listing_id, l.property_id, l.category_type, l.category_main,
       ll.obec_kod, ll.cast_obce_kod
  from autodedup.cluster_members m
  left join public.listings l on l.id = m.listing_id
  left join public.listing_location ll on ll.listing_id = l.id
 where m.generation = %(generation)s::text
 order by m.cluster_key, m.listing_id
"""

# `first_seen_at` names the survivor the merge will keep (`property_identity.survivor_of`).
PROPERTIES_SQL = """
select p.id, p.status, p.category_type, p.category_main, p.first_seen_at
  from public.properties p
 where p.id = any(%(property_ids)s::bigint[])
"""

# EVERY listing on the involved properties, not only the group's members: a merge moves all
# of a property's children, so the negatives, the size, category and scope checks (location
# included, as MEMBERS_SQL reads it) and the carry-along check read the whole set — at plan time
# and again inside each group's transaction. Columns in `apply.Member` order.
PROPERTY_LISTINGS_SQL = """
select l.id, l.property_id, l.category_type, l.category_main, ll.obec_kod, ll.cast_obce_kod
  from public.listings l
  left join public.listing_location ll on ll.listing_id = l.id
 where l.property_id = any(%(property_ids)s::bigint[])
"""

# The apply-time re-check (E903), inside the group's own transaction: the involved properties
# locked in id order (the chokepoint's own FOR UPDATE, taken early and for all of them, so no
# concurrent merge or detach can re-point one between the re-check and the merge), then
# their listings held FOR SHARE, so none is re-categorised or moved away before it commits.
LOCK_PROPERTIES_SQL = """
select p.id, p.status, p.category_type, p.category_main
  from public.properties p
 where p.id = any(%(property_ids)s::bigint[])
 order by p.id
   for update
"""

# `for share of l`: the listings only — a lock may not reach the nullable side of the outer
# join, and the resolver's writes to listing_location are not held up by a merge.
LOCK_PROPERTY_LISTINGS_SQL = """
select l.id, l.property_id, l.category_type, l.category_main, ll.obec_kod, ll.cast_obce_kod
  from public.listings l
  left join public.listing_location ll on ll.listing_id = l.id
 where l.property_id = any(%(property_ids)s::bigint[])
 order by l.id
   for share of l
"""

MUST_NOT_LINK_SQL = """
select n.listing_lo, n.listing_hi, n.source
  from autodedup.must_not_link n
 where n.listing_lo = any(%(listing_ids)s::bigint[])
   and n.listing_hi = any(%(listing_ids)s::bigint[])
"""

# Per pair only the NEWEST ruling stands (the lane's must-link read, RT_MUST_LINK_SQL, is the
# same rule): a pair ruled different and later ruled same is no longer a negative, and one ruled
# same and later different is one. `negatives` names the verdicts wanted, of the newest only.
PAIR_VERDICTS_SQL = """
select v.listing_lo, v.listing_hi, v.verdict
  from (select distinct on (x.listing_lo, x.listing_hi)
               x.listing_lo, x.listing_hi, x.verdict
          from autodedup.verdicts x
         where x.kind = 'pair'
           and x.listing_lo = any(%(listing_ids)s::bigint[])
           and x.listing_hi = any(%(listing_ids)s::bigint[])
         order by x.listing_lo, x.listing_hi, x.decided_at desc, x.id desc) v
 where v.verdict = any(%(negatives)s::text[])
"""

# A group ruling is about a SET of listings (E58), so it is fetched by the listings it names,
# never by the key it was taken under: a later group holding that set plus one more listing may
# carry another key and must still be refused. EVERY verdict on a set is read, positive ones
# too: per set only the NEWEST ruling stands, whoever took it, so a set ruled different and
# later ruled same or withdrawn no longer refuses (E903, E920). A row without `member_ids`
# (538 backfilled every earlier ruling; only the old API's migration window can have left one)
# names no set, so only its negatives are read, by key, and refuse that key in EVERY
# generation (fail closed).
CLUSTER_VERDICTS_SQL = """
select v.cluster_key, v.verdict, v.generation, v.member_ids, v.decided_at, v.id
  from autodedup.verdicts v
 where v.kind = 'cluster'
   and ((v.member_ids is not null and v.member_ids && %(listing_ids)s::bigint[])
        or (v.member_ids is null
            and v.verdict = any(%(negatives)s::text[])
            and v.cluster_key = any(%(cluster_keys)s::bigint[])))
"""

# The engine's own history that can refuse a group: a live merge of one of these properties
# (restored since, by `unapply` or by someone else) and a chokepoint refusal of this group's
# MEMBER SET in this generation (a real-time generation's keys move with its groups, A9).
# `undone_by` tells the two restorers apart: only `unapply`'s own stamp is the engine's undo.
LEDGER_HISTORY_SQL = """
select a.generation, a.cluster_key, a.retired_property_id, a.outcome,
       a.undone_at is not null as undone, a.undone_by, a.member_ids
  from autodedup.applied_merges a
 where not a.dry_run
   and ((a.outcome = 'refused'
         and a.generation = %(generation)s::text
         and a.member_ids && %(listing_ids)s::bigint[])
        or (a.outcome = 'applied'
            and a.retired_property_id = any(%(property_ids)s::bigint[])))
"""

# Every engine merge whose group named one of these listings, undone or not. A LIVE one is the
# only thing that lets a merge carry a listing its own group does not hold (E903
# `carries_ungrouped_listings`) and names the engine merge behind a group the operator ruled
# different after it merged. One the engine did NOT undo itself whose members no longer share
# a property was taken apart by someone else: its separated listings are the operator's
# negative, keyed on LISTINGS, so it survives any later re-merge of the properties (E905).
ENGINE_MERGES_SQL = """
select distinct on (a.merge_group_id)
       a.generation, a.cluster_key, a.merge_group_id::text, a.survivor_property_id,
       a.member_ids, a.undone_at is not null as undone, a.undone_by
  from autodedup.applied_merges a
 where not a.dry_run
   and a.outcome = 'applied'
   and a.member_ids && %(listing_ids)s::bigint[]
 order by a.merge_group_id, a.id
"""

# A LATER live engine merge (any generation) with this group's survivor as its own survivor
# or retired property, with when it merged and the placement it recorded. `unapply` names one
# to undo first only where that undo is what this group waits on (E905): one that put more
# listings on a survivor still active and whose own undo would not be refused for moving
# nothing back, or the one whose retirement of the survivor still stands. A later merge
# sharing only listings is neither: undoing this group moves back only what sits on its own
# survivor.
LATER_LIVE_MERGES_SQL = """
select a.generation, a.cluster_key, a.merge_group_id::text, a.survivor_property_id,
       a.retired_property_id, a.applied_at, a.plan_json
  from autodedup.applied_merges a
 where not a.dry_run
   and a.outcome = 'applied'
   and a.undone_at is null
   and a.id > %(after_id)s::bigint
   and (a.survivor_property_id = %(property_id)s::bigint
        or a.retired_property_id = %(property_id)s::bigint)
 order by a.id
"""

LEDGER_INSERT_SQL = """
insert into autodedup.applied_merges (
    run_id, generation, cluster_key, survivor_property_id, retired_property_id,
    merge_group_id, dry_run, outcome, error, listings_moved, member_ids, plan_json
) values (
    %(run_id)s::text, %(generation)s::text, %(cluster_key)s::bigint,
    %(survivor_property_id)s::bigint, %(retired_property_id)s::bigint,
    %(merge_group_id)s::uuid, %(dry_run)s::boolean, %(outcome)s::text, %(error)s::text,
    %(listings_moved)s::integer, %(member_ids)s::bigint[], %(plan_json)s::jsonb
)
"""

# Newest-first, so groups are undone in the reverse of the order they were applied, picked by
# generation (and cluster_key), apply run and time window: every selector given must hold. Every
# row of a group carries the same generation, run, survivor, member set and plan (with the
# property each moved listing sat on when it merged: the adverts the undo's detach loop moves
# back) and the same `applied_at`: now() of the group's transaction, the
# chokepoint's `merged_at`.
UNAPPLY_TARGETS_SQL = """
select a.merge_group_id::text, max(a.generation), a.cluster_key, max(a.survivor_property_id),
       array_agg(a.retired_property_id order by a.id), max(a.id), max(a.member_ids),
       (array_agg(a.plan_json order by a.id))[1], min(a.applied_at)
  from autodedup.applied_merges a
 where not a.dry_run
   and a.outcome = 'applied'
   and a.undone_at is null
   and (%(generation)s::text is null or a.generation = %(generation)s::text)
   and (%(cluster_key)s::bigint is null or a.cluster_key = %(cluster_key)s::bigint)
   and (%(run)s::text is null or a.run_id = %(run)s::text)
   and (%(since)s::timestamptz is null or a.applied_at >= %(since)s::timestamptz)
   and (%(until)s::timestamptz is null or a.applied_at < %(until)s::timestamptz)
 group by a.merge_group_id, a.cluster_key
 order by max(a.id) desc
"""

# Where a group's survivor stands now. Only the chokepoint and `detach_listing` write
# `merged_into` and `merged_at`, and the chokepoint stamps `merged_at` with now() of the
# transaction that also wrote the group's ledger rows (`applied_at`). So a survivor merged into
# a later merge's survivor at that merge's `applied_at` is still retired by it (E905).
PROPERTY_STATE_SQL = """
select p.id, p.status, p.merged_into, p.merged_at
  from public.properties p
 where p.id = any(%(property_ids)s::bigint[])
"""

# Where a group's listings sit now, read before the detach loop and again in the undo's own
# transaction: a member off the survivor means someone else took the merge apart first (E905).
MEMBER_PROPERTIES_SQL = """
select l.id, l.property_id
  from public.listings l
 where l.id = any(%(listing_ids)s::bigint[])
"""

LEDGER_UNDO_SQL = """
update autodedup.applied_merges
   set undone_at = now(),
       undone_by = %(undone_by)s::text,
       undo_result = %(undo_result)s::jsonb
 where merge_group_id = %(merge_group_id)s::uuid
   and not dry_run
   and outcome = 'applied'
   and undone_at is null
"""

# ------------------------------------------------------------------ A9: the lane's reconcile
#
# The real-time lane reconciles the groups a pass re-clustered plus a slice swept past its own
# `rt_reconcile` cursor, under the lane's lease (autodedup/reconcile.py). The plan and the
# merge are the statements above; these are the reads that pick the groups and the one that
# keeps a waiting group from filing the same skip every pass.

RC_SWEEP_SQL = """
select c.cluster_key
  from autodedup.clusters c
 where c.generation = %(generation)s::text
   and c.cluster_key > %(after)s::bigint
 order by c.cluster_key
 limit %(limit)s
"""

RC_CLUSTERS_SQL = """
select c.cluster_key, c.size, c.status, c.min_edge_score, c.model_version, c.feature_version
  from autodedup.clusters c
 where c.generation = %(generation)s::text
   and c.cluster_key = any(%(keys)s::bigint[])
 order by c.cluster_key
"""

# MEMBERS_SQL for the groups the reconcile picked, in the same column order (`apply.Member`).
RC_MEMBERS_SQL = """
select m.cluster_key, m.listing_id, l.property_id, l.category_type, l.category_main,
       ll.obec_kod, ll.cast_obce_kod
  from autodedup.cluster_members m
  left join public.listings l on l.id = m.listing_id
  left join public.listing_location ll on ll.listing_id = l.id
 where m.generation = %(generation)s::text
   and m.cluster_key = any(%(keys)s::bigint[])
 order by m.cluster_key, m.listing_id
"""

# Which group of the generation holds each carried advert (E37 at property grain): the
# reconcile plans a few groups, the refusal reads the whole generation's membership.
RC_GROUP_OF_SQL = """
select m.listing_id, m.cluster_key
  from autodedup.cluster_members m
 where m.generation = %(generation)s::text
   and m.listing_id = any(%(listing_ids)s::bigint[])
"""

# A group qualifies only when its adverts' blocks are FULLY READ: every advert the scope
# snapshot holds there has a fingerprint (the build reached it). A block the lane has not read
# yet may hold a member the group is still missing.
RC_UNREAD_BLOCKS_SQL = """
select distinct s.block_key
  from autodedup.rt_scope_ids s
 where s.generation = %(generation)s::text
   and not exists (select 1
                     from autodedup.rt_fp f
                    where f.generation = s.generation
                      and f.listing_id = s.listing_id)
"""

RC_MEMBER_BLOCKS_SQL = """
select s.listing_id, s.block_key
  from autodedup.rt_scope_ids s
 where s.generation = %(generation)s::text
   and s.listing_id = any(%(listing_ids)s::bigint[])
"""

# The newest `depth` outcomes per member set, ONE per pass (a group with two retired properties
# files two rows in one pass, under one run id): the newest is what a repeated skip or refusal
# is compared with, so the ledger records CHANGES rather than one row a minute (A9); the run of
# failures before it is what quarantines a group the lane cannot merge.
RC_OUTCOME_HISTORY_SQL = """
select h.member_ids, h.outcome, h.error, h.at
  from (select e.member_ids, e.outcome, e.error, e.at,
               row_number() over (partition by e.member_ids order by e.last_id desc) as rn
          from (select a.member_ids, a.run_id,
                       max(a.id)                                    as last_id,
                       max(a.applied_at)                            as at,
                       (array_agg(a.outcome order by a.id desc))[1] as outcome,
                       (array_agg(a.error order by a.id desc))[1]   as error
                  from autodedup.applied_merges a
                 where a.generation = %(generation)s::text
                   and not a.dry_run
                   and a.member_ids && %(listing_ids)s::bigint[]
                 group by a.member_ids, a.run_id) e) h
 where h.rn <= %(depth)s::integer
 order by h.member_ids, h.rn
"""
