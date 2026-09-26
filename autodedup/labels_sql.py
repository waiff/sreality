"""Every statement the `labels` lane runs, as module-level constants (the PREPARE gate).

Five relations, all inside schema `autodedup` (migrations 528/532/533): `verdicts` — what the
operator ruled, at pair grain and at cluster grain — `clusters` + `cluster_members`, which turn
a confirmed group into the member pairs it asserts, `pairs`, which is the engine's own view of
the same pair at read time, and `must_not_link`, the permanent negatives.

Since migration 564 (E299) two more: `operator_merges`, the operator's own Browse merges copied
ONCE into the engine's schema, and `verdicts.operator_merge_group_id`, the column that says which
of those merges a `same` pair ruling was made by.

Nothing here reads a legacy dedup table (ruling D7) — not even `property_merge_events`, which
564 copied so that nothing would ever read it again. ONE statement reads `public`: where each
operator-merge member sits today (its advert row and its live `listing_location`), the reads
PROGRAM.md section 0 permits the engine. Every statement is a SELECT: the lane exports what the
operator already decided and must never be able to change it.

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
#
# `verdict_id` is appended (never reordered) so the lane can ask OPERATOR_MERGE_LINKS_SQL which
# of these rows a Browse merge wrote — read through the id rather than by naming 564's column
# here, because this statement must keep working on a store 564 has not reached yet.
PAIR_VERDICTS_SQL = """
select distinct on (v.listing_lo, v.listing_hi)
       v.listing_lo, v.listing_hi, v.verdict, v.note, v.reasons, v.decided_by, v.decided_at,
       v.id as verdict_id
  from autodedup.verdicts v
 where v.kind = 'pair'
   and v.listing_lo is not null
   and v.listing_hi is not null
 order by v.listing_lo, v.listing_hi, v.decided_at desc, v.id desc
"""

# The cluster-grain half. Scoped to ONE generation because a cluster key is only meaningful
# inside the pass that built it — `score` rebuilds clusters whole, so the same key in g3 and
# g4 is not the same set of listings.
#
# THE SET COMES OFF THE VERDICT (E58, migration 538), not off the clustering. Before 538 this
# joined `autodedup.clusters` by key alone and read TODAY's members, so promoting g5 silently
# changed what a g4 export asserted — and then erased g4's clusters entirely, leaving
# `mode=labels generation=g4` unable to reproduce itself at all. `member_ids` is what the
# operator was looking at; the join to `cluster_members` survives only as the fallback for a
# LEGACY row that carries none, and that fallback is scoped to the asked-for generation.
#
# "DOES THIS RULING APPLY HERE?" IS ANSWERED ON THE SET, exactly as the Groups page and the
# progress strip answer it (`ui_sql._CLUSTER_FROM`). The generation STRING alone is not that
# answer: after 538 all 224 backfilled rulings read `g4`, so a string test would have exported
# ZERO implied labels for g5 — while the UI shows 203 of those rulings applying to g5 groups
# whose membership never moved, and the agreement read counts them. One fact, three readers,
# one test. The string arm stays as a union, not as the test: it is what keeps a pass's own
# rulings exportable when that pass's clusters are not in the store (g4's, until the score lane
# re-persists them), which is the whole point of recording the set.
CLUSTER_VERDICTS_SQL = """
select distinct on (v.cluster_key)
       v.cluster_key, v.verdict, v.note, v.reasons, v.decided_by, v.decided_at,
       v.generation, coalesce(v.member_ids, mem.ids) as member_ids,
       coalesce(array_length(coalesce(v.member_ids, mem.ids), 1), 0) as size
  from autodedup.verdicts v
  left join lateral (
      select array_agg(m.listing_id order by m.listing_id) as ids
        from autodedup.cluster_members m
       where m.generation = %(generation)s::text
         and m.cluster_key = v.cluster_key
  ) mem on true
 where v.kind = 'cluster'
   and v.cluster_key is not null
   and (v.generation = %(generation)s::text
        or (v.member_ids is not null and v.member_ids = mem.ids)
        or (v.generation is null and mem.ids is not null))
 order by v.cluster_key, v.decided_at desc, v.id desc
"""

# Still read, and still ONLY for the sample rank and the run summary: the implied labels come
# off `verdicts.member_ids` above. Scoped to the generation, because a key alone no longer
# names one set of members (migration 538).
CLUSTER_MEMBERS_SQL = """
select m.cluster_key, m.listing_id
  from autodedup.cluster_members m
 where m.generation = %(generation)s::text
   and m.cluster_key = any(%(keys)s::bigint[])
 order by m.cluster_key, m.listing_id
"""

# The engine's view of a labelled pair AT READ TIME: what the stored pass scored, decided and
# clustered it as. It is a snapshot for auditing a label against the engine that saw it, never
# an input to the label itself — a pair the engine never stored is still a label.
#
# SCOPED TO THE GENERATION, and since migration 538 by the pair's OWN column rather than by the
# `(model_version, feature_version)` its clusters happened to carry. `autodedup.pairs` holds
# every pass ever scored — g1 `hand_v1`/1 through g5 `w6_gold`/4 — so an unscoped read hands a
# g2 zone to a g4 question. That is not a cosmetic slip: it decides whether a label sits in the
# band this generation pays a judge for, and W6 found 153 of 444 explicit labels carrying a
# zone no g4 pass ever assigned. The old spelling ALSO broke the moment a generation's clusters
# were re-stamped away — g4's were — because the version pair it derived the scope from came
# from a table that no longer described g4.
ENGINE_PAIRS_SQL = """
select p.listing_lo, p.listing_hi, p.score, p.zone, p.decision, p.guard_veto, p.cluster_key,
       p.model_version, p.feature_version, p.families, p.decided_at
  from autodedup.pairs p
 where p.generation = %(generation)s::text
   and (p.listing_lo, p.listing_hi) in (
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


# --- the operator's Browse merges (migration 564, E299) ------------------------------------

# Probed before either object is read. The lane is dispatched from `main`, and `main` can
# carry this code before 564 is applied to the store: an export that died on a missing
# column would lose every label it could have written, so the group file is skipped — and
# the summary says so — instead.
OPERATOR_MERGES_PRESENT_SQL = """
select to_regclass('autodedup.operator_merges') is not null
   and exists (
         select 1
           from information_schema.columns c
          where c.table_schema = 'autodedup'
            and c.table_name = 'verdicts'
            and c.column_name = 'operator_merge_group_id'
       ) as present
"""

# Every copied group, whatever its status: a corrected group is still the operator's record,
# and the consumer decides what an `undone` or `withdrawn` row counts for.
OPERATOR_MERGES_SQL = """
select m.merge_group_id, m.merged_at, m.survivor_property_id, m.retired_property_ids,
       m.member_ids, m.member_sides, m.member_property_ids, m.n_pairs, m.decided_by,
       m.source, m.status, m.status_note, m.status_at, m.copied_at
  from autodedup.operator_merges m
 order by m.merged_at, m.merge_group_id
"""

# The pair-grain link: which `same` pair rulings a Browse merge wrote. Joined to
# PAIR_VERDICTS_SQL's rows by id in Python, so a pair whose LATEST ruling was typed by hand
# after the merge reads as that ruling, not as the merge.
OPERATOR_MERGE_LINKS_SQL = """
select v.id as verdict_id, v.operator_merge_group_id
  from autodedup.verdicts v
 where v.kind = 'pair'
   and v.operator_merge_group_id is not null
 order by v.id
"""

# Where each member IS today: its advert row (portal, category, current property) and its
# live `listing_location` row (primary key listing_id), the blocks the export lane is
# dispatched by. Read at export time, never stored: this is what tells the operator which
# cohort to export so a group can be measured at all.
OPERATOR_MERGE_MEMBERS_SQL = """
select l.id as listing_id, l.source, l.category_type, l.category_main, l.property_id,
       ll.obec_kod, ll.cast_obce_kod
  from public.listings l
  left join public.listing_location ll on ll.listing_id = l.id
 where l.id = any(%(ids)s::bigint[])
 order by l.id
"""
