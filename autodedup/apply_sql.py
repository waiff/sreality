"""Every statement the A1 apply path runs (PROGRAM.md E900-E906), as constants.

Reads: the two `app_settings` switches, one generation's groups and members from schema
`autodedup`, the member listings' `property_id` and categories and the involved properties
(with their asset links, and those of every property merged into them) from `public` (the
same facts the chokepoint itself re-checks), the
operator's negatives (`verdicts`, `must_not_link`) and the engine's own apply ledger and
unapplied-generation stamps. NOTHING here reads
`public.property_merge_events` (D7) — the one statement that names it is a write-only stamp
scoped to a merge group this path just created.

Every nullable parameter carries an explicit cast: psycopg sends no type OID for a Python
`None`, so an uncast NULL fails Parse with 42P18.
"""

from __future__ import annotations

# The operator's two switches (migration 558 seeds both OFF). `public.app_settings`, not
# `autodedup.settings`: /settings edits the former, and a kill switch must be reachable there.
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

# `asset_id` is the operator's Browse-side "different units in one building, do not collapse"
# (migration 224): two involved properties sharing one refuse the group (E903).
PROPERTIES_SQL = """
select p.id, p.status, p.category_type, p.category_main, p.first_seen_at, p.asset_id
  from public.properties p
 where p.id = any(%(property_ids)s::bigint[])
"""

# The asset links a merge would otherwise lose: every property merged INTO one of these,
# followed down `merged_into` (indexed, migration 100), carries its `asset_id` still — the
# chokepoint does not move it onto the survivor. Each is counted as the involved property's own
# link, so a unit the operator asset-linked stays "different units" after an earlier merge
# retired it (E903). A `properties` read only, never `property_merge_events` (D7).
ABSORBED_ASSETS_SQL = """
with recursive absorbed(root, id, depth) as (
    select p.id, p.id, 0
      from public.properties p
     where p.id = any(%(property_ids)s::bigint[])
    union all
    select a.root, q.id, a.depth + 1
      from public.properties q
      join absorbed a on q.merged_into = a.id
     where a.depth < 20
)
select a.root, q.asset_id
  from absorbed a
  join public.properties q on q.id = a.id
 where q.asset_id is not null
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
# concurrent merge or unmerge can re-point one between the re-check and the merge), then
# their listings held FOR SHARE, so none is re-categorised or moved away before it commits.
LOCK_PROPERTIES_SQL = """
select p.id, p.status, p.category_type, p.category_main, p.first_seen_at, p.asset_id
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
select a.generation, a.cluster_key, a.survivor_property_id, a.retired_property_id,
       a.outcome, a.undone_at is not null as undone, a.undone_by
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

# A LATER live engine merge (any generation) that shares this group's survivor as its own
# survivor or retired property, or any of its listings: undoing this group first would leave
# listings on one property that no generation ever grouped, so `unapply` names it to undo
# first (E905).
LATER_LIVE_MERGES_SQL = """
select a.generation, a.cluster_key, a.merge_group_id::text
  from autodedup.applied_merges a
 where not a.dry_run
   and a.outcome = 'applied'
   and a.undone_at is null
   and a.id > %(after_id)s::bigint
   and (a.survivor_property_id = %(property_id)s::bigint
        or a.retired_property_id = %(property_id)s::bigint
        or a.member_ids && %(member_ids)s::bigint[])
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

# E902: WRITE-ONLY, and only rows of a group this run created in the same transaction.
STAMP_GENERATION_SQL = """
update public.property_merge_events
   set generation = %(stamp)s::text
 where merge_group_id = %(merge_group_id)s::uuid
   and generation is null
"""

# Newest-first, so a generation is undone in the reverse of the order it was applied. Every
# row of a group carries the same survivor and member set.
UNAPPLY_TARGETS_SQL = """
select a.merge_group_id::text, a.cluster_key, max(a.survivor_property_id),
       array_agg(a.retired_property_id order by a.id), max(a.id), max(a.member_ids)
  from autodedup.applied_merges a
 where a.generation = %(generation)s::text
   and not a.dry_run
   and a.outcome = 'applied'
   and a.undone_at is null
   and (%(cluster_key)s::bigint is null or a.cluster_key = %(cluster_key)s::bigint)
 group by a.merge_group_id, a.cluster_key
 order by max(a.id) desc
"""

# Where a group's members sit now, read in the undo's own transaction before `unmerge_group`:
# a member off the survivor means someone else took the merge apart first (E905).
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

# E905: a WHOLE-generation `unapply` stamps the generation first, and every later plan of it
# refuses every group — the ones it undid and the ones it never reached (deferred under the
# run cap, skipped for a transient reason) — until an apply dispatched with `reapply=1`
# releases the stamp. An unapply scoped to one `cluster_key` writes no stamp.
UNAPPLIED_GENERATION_SQL = """
select u.id, u.undone_by, u.unapplied_at, u.released_at is not null as released
  from autodedup.unapplied_generations u
 where u.generation = %(generation)s::text
 order by u.id
"""

STAMP_UNAPPLIED_SQL = """
insert into autodedup.unapplied_generations (generation, run_id, undone_by)
values (%(generation)s::text, %(run_id)s::text, %(undone_by)s::text)
"""

RELEASE_UNAPPLIED_SQL = """
update autodedup.unapplied_generations
   set released_at = now(),
       released_by = %(released_by)s::text
 where id = any(%(ids)s::bigint[])
   and released_at is null
"""
