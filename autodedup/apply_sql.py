"""Every statement the A1 apply path runs (PROGRAM.md E900-E906), as constants.

Reads: the `app_settings` scope row, one generation's groups and members from schema
`autodedup`, the member listings' `property_id` and categories and the involved properties from
`public` (the same facts the chokepoint itself re-checks), the operator's negatives
(`verdicts`, `must_not_link`) and the engine's own apply ledger. NOTHING here reads or writes
`public.property_merge_events` (D7): only the chokepoint writes it.

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
select c.cluster_key, c.size, c.status, c.block_key, c.block_grain,
       c.category_main, c.category_type, c.min_edge_score,
       c.model_version, c.feature_version
  from autodedup.clusters c
 where c.generation = %(generation)s::text
 order by c.cluster_key
"""

# A member whose listing row is missing, or that has no property yet (rule 19: new rows land
# with a NULL property_id until maintenance attaches a singleton), comes back with NULLs.
MEMBERS_SQL = """
select m.cluster_key, m.listing_id, l.property_id, l.category_type, l.category_main
  from autodedup.cluster_members m
  left join public.listings l on l.id = m.listing_id
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
# of a property's children, so the negatives, the size, category and scope checks and the
# carry-along check read the whole set — at plan time and again inside each group's transaction.
PROPERTY_LISTINGS_SQL = """
select l.property_id, l.id, l.category_type, l.category_main
  from public.listings l
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

LOCK_PROPERTY_LISTINGS_SQL = """
select l.property_id, l.id, l.category_type, l.category_main
  from public.listings l
 where l.property_id = any(%(property_ids)s::bigint[])
 order by l.id
   for share
"""

MUST_NOT_LINK_SQL = """
select n.listing_lo, n.listing_hi, n.source
  from autodedup.must_not_link n
 where n.listing_lo = any(%(listing_ids)s::bigint[])
   and n.listing_hi = any(%(listing_ids)s::bigint[])
"""

PAIR_VERDICTS_SQL = """
select v.listing_lo, v.listing_hi, v.verdict
  from autodedup.verdicts v
 where v.kind = 'pair'
   and v.verdict = any(%(negatives)s::text[])
   and v.listing_lo = any(%(listing_ids)s::bigint[])
   and v.listing_hi = any(%(listing_ids)s::bigint[])
"""

# A group ruling is about a SET of listings (E58), so it is fetched by the listings it names,
# never by the key it was taken under: a later group holding that set plus one more listing may
# carry another key and must still be refused. EVERY verdict on a set is read, positive ones
# too: per set and per operator only the NEWEST ruling stands, so a set ruled different and
# later ruled same by the same operator no longer refuses (E903). A row without `member_ids`
# (538 backfilled every earlier ruling; only the old API's migration window can have left one)
# names no set, so only its negatives are read, by key, and refuse that key in EVERY
# generation (fail closed).
CLUSTER_VERDICTS_SQL = """
select v.cluster_key, v.verdict, v.generation, v.member_ids, v.decided_by, v.decided_at, v.id
  from autodedup.verdicts v
 where v.kind = 'cluster'
   and ((v.member_ids is not null and v.member_ids && %(listing_ids)s::bigint[])
        or (v.member_ids is null
            and v.verdict = any(%(negatives)s::text[])
            and v.cluster_key = any(%(cluster_keys)s::bigint[])))
"""

# The engine's own history that can refuse a group: a live merge of one of these properties
# (restored since, by `unapply` or by someone else) and a chokepoint refusal of this group.
# `undone_by` tells the two restorers apart: only `unapply`'s own stamp is the engine's undo.
LEDGER_HISTORY_SQL = """
select a.generation, a.cluster_key, a.retired_property_id, a.outcome,
       a.undone_at is not null as undone, a.undone_by
  from autodedup.applied_merges a
 where not a.dry_run
   and a.outcome in ('applied', 'refused')
   and (a.generation = %(generation)s::text
        or a.retired_property_id = any(%(property_ids)s::bigint[]))
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

# Where a group's survivor and retired properties stand now. Only the chokepoint and
# `detach_listing` write `merged_into` and `merged_at`, and the chokepoint stamps `merged_at`
# with now() of the transaction that also wrote the group's ledger rows (`applied_at`). So a
# retired property merged into the survivor at the group's `applied_at` means the merge
# stands; one active again, merged on into another property, or merged back into the survivor
# at another time (by hand, after an undo) was restored by a detach of one of its adverts (E905).
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
