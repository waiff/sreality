"""Every statement the W30 apply path runs (PROGRAM.md E300-E306), as constants.

Reads: the two `app_settings` switches, one generation's groups and members from schema
`autodedup`, the member listings' `property_id` and categories and the involved properties
from `public` (the same facts the chokepoint itself re-checks), the operator's negatives
(`verdicts`, `must_not_link`) and the engine's own apply ledger. NOTHING here reads
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
# carry another key and must still be refused. A row without `member_ids` (538 backfilled every
# earlier ruling; only the old API's migration window can have left one) names no set, so it
# is fetched by key and refuses that key in EVERY generation (fail closed).
CLUSTER_VERDICTS_SQL = """
select v.cluster_key, v.verdict, v.generation, v.member_ids
  from autodedup.verdicts v
 where v.kind = 'cluster'
   and v.verdict = any(%(negatives)s::text[])
   and ((v.member_ids is not null and v.member_ids && %(listing_ids)s::bigint[])
        or (v.member_ids is null and v.cluster_key = any(%(cluster_keys)s::bigint[])))
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

# Every live engine merge whose group named one of these listings: the only thing that lets
# a merge carry a listing its own group does not hold (E303 `carries_ungrouped_listings`), and
# what names the engine merge behind a group the operator ruled different after it merged.
LIVE_ENGINE_MERGES_SQL = """
select distinct on (a.merge_group_id)
       a.generation, a.cluster_key, a.merge_group_id::text, a.survivor_property_id,
       a.member_ids
  from autodedup.applied_merges a
 where not a.dry_run
   and a.outcome = 'applied'
   and a.undone_at is null
   and a.member_ids && %(listing_ids)s::bigint[]
 order by a.merge_group_id, a.id
"""

# A live engine merge that retired this property: what `unapply` must undo FIRST before it can
# undo a group whose survivor has since been merged away.
LIVE_RETIRING_SQL = """
select a.generation, a.cluster_key, a.retired_property_id
  from autodedup.applied_merges a
 where not a.dry_run
   and a.outcome = 'applied'
   and a.undone_at is null
   and a.retired_property_id = any(%(property_ids)s::bigint[])
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

# E302: WRITE-ONLY, and only rows of a group this run created in the same transaction.
STAMP_GENERATION_SQL = """
update public.property_merge_events
   set generation = %(stamp)s::text
 where merge_group_id = %(merge_group_id)s::uuid
   and generation is null
"""

# Newest-first, so a generation is undone in the reverse of the order it was applied.
UNAPPLY_TARGETS_SQL = """
select a.merge_group_id::text, a.cluster_key, max(a.survivor_property_id),
       array_agg(a.retired_property_id order by a.id), max(a.id)
  from autodedup.applied_merges a
 where a.generation = %(generation)s::text
   and not a.dry_run
   and a.outcome = 'applied'
   and a.undone_at is null
   and (%(cluster_key)s::bigint is null or a.cluster_key = %(cluster_key)s::bigint)
 group by a.merge_group_id, a.cluster_key
 order by max(a.id) desc
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
