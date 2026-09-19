"""Every statement the `labels` lane runs, as module-level constants (the PREPARE gate).

Five relations, all inside schema `autodedup` (migrations 528/532/533): `verdicts` — what the
operator ruled, at pair grain and at cluster grain — `clusters` + `cluster_members`, which turn
a confirmed group into the member pairs it asserts, `pairs`, which is the engine's own view of
the same pair at read time, and `must_not_link`, the permanent negatives.

Nothing here reads a `public.*` relation and nothing reads a legacy dedup table (ruling D7).
Every statement is a SELECT: the lane exports what the operator already decided and must never
be able to change it.

`distinct on` is the latest-wins reader in both places. A pair can carry one row per
`decided_by` (migration 528's partial unique index is on `(kind, listing_lo, listing_hi,
decided_by)`), and a cluster can be re-ruled; the newest decision is the operator's current
belief, so the ordering is `decided_at desc, id desc` — `id` breaks the tie two verdicts saved
inside the same card save would otherwise leave to chance.
"""

from __future__ import annotations

# The lane's deliverable IS the store's content, so an absent store is fatal, not best effort.
STORE_PRESENT_SQL = """
select to_regclass('autodedup.verdicts')       is not null
   and to_regclass('autodedup.clusters')       is not null
   and to_regclass('autodedup.cluster_members') is not null
   and to_regclass('autodedup.must_not_link')  is not null as present
"""

# Every explicitly ruled pair, latest verdict per pair. Deliberately NOT scoped to a
# generation: an operator ruling is about two adverts, not about the pass that proposed them,
# and a pair ruled under g3 says exactly as much about g4's model as one ruled today.
PAIR_VERDICTS_SQL = """
select distinct on (v.listing_lo, v.listing_hi)
       v.listing_lo, v.listing_hi, v.verdict, v.note, v.reasons, v.decided_by, v.decided_at
  from autodedup.verdicts v
 where v.kind = 'pair'
   and v.listing_lo is not null
   and v.listing_hi is not null
 order by v.listing_lo, v.listing_hi, v.decided_at desc, v.id desc
"""

# The cluster-grain half. Scoped to ONE generation because a cluster key is only meaningful
# inside the pass that built it — `score` rebuilds clusters whole, so the same key in g3 and
# g4 is not the same set of listings.
CLUSTER_VERDICTS_SQL = """
select distinct on (v.cluster_key)
       v.cluster_key, v.verdict, v.note, v.reasons, v.decided_by, v.decided_at,
       c.size, c.generation
  from autodedup.verdicts v
  join autodedup.clusters c on c.cluster_key = v.cluster_key
 where v.kind = 'cluster'
   and v.cluster_key is not null
   and c.generation = %(generation)s::text
 order by v.cluster_key, v.decided_at desc, v.id desc
"""

CLUSTER_MEMBERS_SQL = """
select m.cluster_key, m.listing_id
  from autodedup.cluster_members m
 where m.cluster_key = any(%(keys)s::bigint[])
 order by m.cluster_key, m.listing_id
"""

# The engine's view of a labelled pair AT READ TIME: what the stored pass scored, decided and
# clustered it as. It is a snapshot for auditing a label against the engine that saw it, never
# an input to the label itself — a pair the engine never stored is still a label.
#
# SCOPED TO THE GENERATION, via the (model_version, feature_version) its clusters carry.
# `autodedup.pairs` accumulates every pass ever scored — g1 `hand_v1`/1 through g4 `w5_gold`/4 —
# and has no generation column, so an unscoped read hands a g2 zone to a g4 question. That is
# not a cosmetic slip: it is what decides whether a label sits in the band this generation pays
# a judge for, and W6 found 153 of 444 explicit labels carrying a zone no g4 pass ever assigned.
ENGINE_PAIRS_SQL = """
with generation as (
    select distinct model_version, feature_version
      from autodedup.clusters
     where generation = %(generation)s::text
)
select p.listing_lo, p.listing_hi, p.score, p.zone, p.decision, p.guard_veto, p.cluster_key,
       p.model_version, p.feature_version, p.families, p.decided_at
  from autodedup.pairs p
  join generation g
    on g.model_version = p.model_version
   and g.feature_version = p.feature_version
 where (p.listing_lo, p.listing_hi) in (
         select lo, hi from unnest(%(los)s::bigint[], %(his)s::bigint[]) as pair(lo, hi)
       )
"""

# Only the operator's own rows. A `guard`/`model`/`llm` veto is the engine talking to itself;
# this artifact is the operator's testimony.
MUST_NOT_LINK_SQL = """
select n.listing_lo, n.listing_hi, n.source, n.reason, n.created_at
  from autodedup.must_not_link n
 where n.source = 'operator'
 order by n.listing_lo, n.listing_hi
"""

GENERATION_CLUSTERS_SQL = """
select count(*) as n_clusters, coalesce(max(c.size), 0) as max_size
  from autodedup.clusters c
 where c.generation = %(generation)s::text
"""


# THE SEEDED SAMPLE ORDER, AS A RANK (D6/E55). The validation UI offers groups in
# `md5(cluster_key || seed)` order (`ui_sql.GROUPS_RANDOM_SQL`) so a session's sample is
# unbiased and stable; "the first 100 groups" is the population every unbiased precision
# number in this programme is measured on. That population is reconstructible only while the
# same seed is re-run against the same cluster set, which is a live-store fact — so the rank
# travels IN the artifact: an implied label carries where its group sat in the sample order,
# and a later fit can rebuild the sample from the file alone.
#
# The window is over the WHOLE generation, not over the labelled subset: a rank that counted
# only confirmed groups would renumber itself every time the operator ruled one more.
SAMPLE_RANK_SQL = """
with ranked as (
    select c.cluster_key,
           row_number() over (
               order by md5(c.cluster_key::text || %(seed)s::text) asc, c.cluster_key asc
           ) as sample_rank
      from autodedup.clusters c
     where c.generation = %(generation)s::text
)
select r.cluster_key, r.sample_rank
  from ranked r
 where r.cluster_key = any(%(keys)s::bigint[])
"""
