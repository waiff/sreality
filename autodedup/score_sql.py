"""Every statement the `score` lane runs, as module-level constants (the PREPARE gate).

Four relations and nothing else, all of them inside schema `autodedup` (migration 528):
`runs` (one row per pass), `pairs`, `clusters` + `cluster_members` + `cluster_conflicts`.
Two reads come along for the ride — `must_not_link`, so an operator's negative verdict binds
the NEXT generation's clustering, and `judgements`, so a cluster row can say how many of its
edges an LLM has already arbitrated. Ruling D4: nothing here touches a `public.*` table.

Nullable parameters carry explicit casts: psycopg sends no type OID for a Python `None`, so
an uncast NULL parameter fails Parse with 42P18. Almost every column the engine writes is
legitimately null on some pair (a vetoed pair has no cluster, a single-block cluster that
spans two towns has no one `block_key`), so the casts are the rule here, not the exception.

`clusters.block_grain` (migration 529) travels with `block_key`: the engine's block key is
grain-prefixed text (`c490245` / `o563510`) and the column is a bigint, so the letter is stored
beside the code rather than thrown away — the two together are the block, one alone is not.

`applied_merge_group` is NEVER listed — not in the insert, not in the `do update set`. It is
the write path's own column (E40) and shadow mode leaves it alone; naming it in the upsert
would let a re-score silently clear a stamp the engine did not place.
"""

from __future__ import annotations

# The pass refuses to start when its own store is absent: unlike the progress ledger, which is
# best effort, persistence IS this lane's deliverable.
STORE_PRESENT_SQL = """
select to_regclass('autodedup.runs')     is not null
   and to_regclass('autodedup.pairs')    is not null
   and to_regclass('autodedup.clusters') is not null as present
"""

# Opened BEFORE the engine runs, on what is known without it: the fingerprint and the
# parameters. The engine phase is the longest and the likeliest to die, and a pass that dies
# without a row is a pass the progress page reports as the previous success.
RUN_START_SQL = """
insert into autodedup.runs (mode, status, fingerprint, params)
values ('score', 'running', %(fingerprint)s::text, %(params)s::jsonb)
returning id
"""

# One statement for both terminal outcomes. `cohort` and `stats` are coalesced — they are only
# knowable once the dataset is loaded and the engine has run, so a failure before that keeps
# whatever the row already had — and `error` is written unconditionally: a re-used run row that
# succeeded on a retry must not keep the first attempt's error text.
RUN_FINISH_SQL = """
update autodedup.runs
   set status      = %(status)s,
       cohort      = coalesce(%(cohort)s::jsonb, cohort),
       stats       = coalesce(%(stats)s::jsonb, stats),
       error       = %(error)s::text,
       finished_at = now()
 where id = %(id)s::bigint
returning id
"""

# E27/E33: the operator's permanent negatives are an INPUT to clustering, not a report on it.
# The table is small by construction (one row per operator "different" verdict), so it is read
# whole rather than scoped to the cohort.
MUST_NOT_LINK_SQL = """
select listing_lo, listing_hi
  from autodedup.must_not_link
"""

# Latest-wins per unordered pair (the table's own primary key). A re-score of the same cohort
# overwrites the previous verdict rather than accumulating history — `listing_snapshots` this
# is not; the history of a decision is the `runs` row that produced it.
PAIR_UPSERT_SQL = """
insert into autodedup.pairs (
    listing_lo, listing_hi, probes, families, features, score, zone, decision,
    guard_veto, cluster_key, feature_version, model_version, decided_at
) values (
    %(listing_lo)s::bigint, %(listing_hi)s::bigint, %(probes)s::text[],
    %(families)s::smallint, %(features)s::jsonb, %(score)s::real, %(zone)s::text,
    %(decision)s::text, %(guard_veto)s::text, %(cluster_key)s::bigint,
    %(feature_version)s::smallint, %(model_version)s::text, now()
)
on conflict (listing_lo, listing_hi) do update set
    probes          = excluded.probes,
    families        = excluded.families,
    features        = excluded.features,
    score           = excluded.score,
    zone            = excluded.zone,
    decision        = excluded.decision,
    guard_veto      = excluded.guard_veto,
    cluster_key     = excluded.cluster_key,
    feature_version = excluded.feature_version,
    model_version   = excluded.model_version,
    decided_at      = excluded.decided_at
"""

# Which of this generation's cluster edges already carry an LLM verdict. The two arrays are
# zipped by `unnest`, not crossed: `lo = any(los) and hi = any(his)` would read back the whole
# product of the edge list instead of the edges themselves (the shape judge_sql documents).
JUDGED_EDGES_SQL = """
select listing_lo, listing_hi
  from autodedup.judgements
 where (listing_lo, listing_hi) in (
         select lo, hi
           from unnest(%(los)s::bigint[], %(his)s::bigint[]) as pair(lo, hi)
       )
 group by listing_lo, listing_hi
"""

# A generation is rebuilt WHOLE — delete then insert — because a cluster that lost a member is
# not an update of anything: union-find reassigns identities, and an upsert alone would leave
# yesterday's members attached to today's clusters.
#
# TWO disjuncts, not one. `clusters` is keyed on `cluster_key` ALONE, so a key first written
# under `g1` and re-written under `g2` is the SAME row, silently re-labelled by the upsert —
# scoping the sweep on `generation` alone would then match nothing and leave g1's members
# attached to g2's cluster. The incoming key set closes that: whatever this pass is about to
# write is swept whichever generation last claimed it. Members are swept by key directly (not
# only through their cluster row) so an orphan from an interrupted pass is collected too.
CLUSTER_MEMBERS_DELETE_SQL = """
delete from autodedup.cluster_members m
 where m.cluster_key = any(%(keys)s::bigint[])
    or exists (
         select 1
           from autodedup.clusters c
          where c.cluster_key = m.cluster_key
            and c.generation  = %(generation)s
       )
"""

# `cluster_conflicts` carries no `generation` column of its own (migration 528), so the lane
# stamps the generation into `detail` and scopes the sweep by it. A conflict row written by
# any other producer — one without that stamp — is left alone.
CLUSTER_CONFLICTS_DELETE_SQL = """
delete from autodedup.cluster_conflicts
 where detail ->> 'generation' = %(generation)s
"""

CLUSTERS_DELETE_SQL = """
delete from autodedup.clusters
 where generation = %(generation)s
    or cluster_key = any(%(keys)s::bigint[])
"""

# `pairs` survives the generation rebuild (it is latest-wins and generation-free), so a pair
# whose cluster was just deleted and not re-created would keep pointing at a cluster row that
# no longer exists — and the residual view would read that dangling key as "already clustered".
# Run LAST, after the new clusters are in, so it only ever clears what really vanished.
PAIR_CLUSTER_ORPHAN_CLEAR_SQL = """
update autodedup.pairs p
   set cluster_key = null
 where p.cluster_key is not null
   and not exists (
         select 1 from autodedup.clusters c where c.cluster_key = p.cluster_key
       )
"""

# `property_id` is never written: nothing is applied in shadow mode (D4), and a cluster that
# claims a property it never merged is the one row that could mislead a later write path.
CLUSTER_INSERT_SQL = """
insert into autodedup.clusters (
    cluster_key, generation, size, block_key, block_grain, cat_group, category_main,
    category_type, area_min, area_max, sources, medoid_listing_id, min_edge_score,
    mean_edge_score,
    n_judged_edges, n_certificate_edges, evidence_families, max_gap_days,
    shared_photo_warning, status, model_version, feature_version, last_changed_at
) values (
    %(cluster_key)s::bigint, %(generation)s, %(size)s::integer, %(block_key)s::bigint,
    %(block_grain)s::text, %(cat_group)s::text, %(category_main)s::text,
    %(category_type)s::text, %(area_min)s::numeric, %(area_max)s::numeric,
    %(sources)s::text[],
    %(medoid_listing_id)s::bigint, %(min_edge_score)s::real, %(mean_edge_score)s::real,
    %(n_judged_edges)s::integer, %(n_certificate_edges)s::integer,
    %(evidence_families)s::smallint, %(max_gap_days)s::integer,
    %(shared_photo_warning)s::boolean, %(status)s, %(model_version)s::text,
    %(feature_version)s::smallint, now()
)
on conflict (cluster_key) do update set
    generation           = excluded.generation,
    size                 = excluded.size,
    block_key            = excluded.block_key,
    block_grain          = excluded.block_grain,
    cat_group            = excluded.cat_group,
    category_main        = excluded.category_main,
    category_type        = excluded.category_type,
    area_min             = excluded.area_min,
    area_max             = excluded.area_max,
    sources              = excluded.sources,
    medoid_listing_id    = excluded.medoid_listing_id,
    min_edge_score       = excluded.min_edge_score,
    mean_edge_score      = excluded.mean_edge_score,
    n_judged_edges       = excluded.n_judged_edges,
    n_certificate_edges  = excluded.n_certificate_edges,
    evidence_families    = excluded.evidence_families,
    max_gap_days         = excluded.max_gap_days,
    shared_photo_warning = excluded.shared_photo_warning,
    status               = excluded.status,
    model_version        = excluded.model_version,
    feature_version      = excluded.feature_version,
    last_changed_at      = now()
"""

CLUSTER_MEMBER_INSERT_SQL = """
insert into autodedup.cluster_members (cluster_key, listing_id, joined_via_lo, joined_via_hi)
values (%(cluster_key)s::bigint, %(listing_id)s::bigint, %(joined_via_lo)s::bigint,
        %(joined_via_hi)s::bigint)
on conflict (cluster_key, listing_id) do update set
    joined_via_lo = excluded.joined_via_lo,
    joined_via_hi = excluded.joined_via_hi
"""

CLUSTER_CONFLICT_INSERT_SQL = """
insert into autodedup.cluster_conflicts (
    kind, cluster_key_a, cluster_key_b, listing_lo, listing_hi, invariant, detail
) values (
    %(kind)s, %(cluster_key_a)s::bigint, %(cluster_key_b)s::bigint, %(listing_lo)s::bigint,
    %(listing_hi)s::bigint, %(invariant)s::text, %(detail)s::jsonb
)
"""
