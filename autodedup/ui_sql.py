"""Every statement the AUTODEDUP validation UI reads (PROGRAM.md §12, W5), as constants.

The groups / residual / pair views are the operator's side of the engine: they read
`autodedup.clusters`, `cluster_members`, `cluster_conflicts`, `pairs`, `judgements` and
`verdicts` (migration 528) and join `public.listings` + `public.images` ONLY for display.
Nothing here writes a production table — the two write statements at the bottom land in
`autodedup.verdicts` and `autodedup.must_not_link`, which is the operator feedback loop of §9.

PII (E28). `listings` carries `broker_name` / `broker_email` / `broker_phone` (migration 025);
NOT ONE of them is selected by any statement in this module, and every advert string —
description AND title — travels through the judge's own scrub (`listing_digest` /
`scrubbed_text`, one set of regexes, two lengths) before it reaches a response. A future
column added to `LISTING_DETAIL_COLUMNS` has to be checked against that rule by hand — the
select lists here are explicit for exactly that reason, never `l.*`.

Filters are nullable parameters, never interpolated text: a filter the operator did not set
is a `%(name)s::type IS NULL OR …` arm, so ONE prepared plan serves every combination and no
predicate can arrive off the wire. Every nullable parameter carries an explicit `::cast` —
psycopg sends no type OID for a Python `None`, so an uncast NULL fails Parse with 42P18.

Keyset paging, never OFFSET: each list statement's cursor is the row-value of its own sort
key, so a page boundary cannot repeat or skip a row under a concurrent rebuild.

The `certificate` of a pair is not a column: `decide.decide_pair` writes the reason string
`certificate:K-A` (or `certificate:K-A:evidence_gate`), so `split_part(decision, ':', 1) =
'certificate'` is how a certificate edge is counted, and the route parses the code out of the
same string. A `LIKE 'certificate:%'` would be the obvious spelling and is deliberately NOT
used: a bare `%` inside a module-level `*_SQL` constant is what
tests/test_sql_placeholders.py exists to reject.
"""

from __future__ import annotations

# ---------------------------------------------------------------- clusters (the groups view)

CLUSTER_COLUMNS: tuple[str, ...] = (
    "cluster_key",
    "generation",
    "size",
    "block_key",
    "cat_group",
    "category_main",
    "category_type",
    "area_min",
    "area_max",
    "sources",
    "medoid_listing_id",
    "min_edge_score",
    "mean_edge_score",
    "n_judged_edges",
    "n_certificate_edges",
    "evidence_families",
    "max_gap_days",
    "shared_photo_warning",
    "status",
    "model_version",
    "feature_version",
    "first_built_at",
    "last_changed_at",
    "verdict",
    "verdict_note",
    "verdict_reasons",
    "verdict_decided_by",
    "verdict_decided_at",
)

# The FROM is its own constant so the COUNT behind "20 of N" runs the SAME joins as the page
# it counts. The verdict LATERAL is not decoration — `_CLUSTER_WHERE` filters on it — and a
# count that dropped it would answer a different question from the list above it.
_CLUSTER_FROM = """
FROM autodedup.clusters c
LEFT JOIN LATERAL (
    SELECT vv.verdict, vv.note, vv.reasons, vv.decided_by, vv.decided_at
      FROM autodedup.verdicts vv
     WHERE vv.kind = 'cluster' AND vv.cluster_key = c.cluster_key
     ORDER BY vv.decided_at DESC, vv.id DESC
     LIMIT 1
) v ON true
"""

_CLUSTER_SELECT = (
    """
SELECT
    c.cluster_key, c.generation, c.size, c.block_key, c.cat_group, c.category_main,
    c.category_type, c.area_min, c.area_max, c.sources, c.medoid_listing_id,
    c.min_edge_score, c.mean_edge_score, c.n_judged_edges, c.n_certificate_edges,
    c.evidence_families, c.max_gap_days, c.shared_photo_warning, c.status,
    c.model_version, c.feature_version, c.first_built_at, c.last_changed_at,
    v.verdict, v.note, v.reasons, v.decided_by, v.decided_at
"""
    + _CLUSTER_FROM
)

# `verdict = 'unreviewed'` is the ABSENCE of a row, which is why the verdict filter is one arm
# of this predicate and not a join condition: filtering in the LATERAL would hand back every
# cluster with its verdict blanked instead of the clusters that carry that verdict.
_CLUSTER_WHERE = """
WHERE c.generation = %(generation)s::text
  AND (%(block)s::bigint IS NULL OR c.block_key = %(block)s::bigint)
  -- A BLOCK IS A CODE AND A GRAIN. `block_key` is a bigint and a cast-obce code shares its
  -- number space with an obec code, so the code alone can name two different blocks —
  -- exactly the conflation migration 529 added `block_grain` to end. This arm is the filter
  -- half of that fix: without it the picker offers two options that mean one query.
  AND (%(block_grain)s::text IS NULL OR c.block_grain = %(block_grain)s::text)
  AND (%(source)s::text IS NULL OR %(source)s::text = any(c.sources))
  AND (%(category_main)s::text IS NULL OR c.category_main = %(category_main)s::text)
  AND (%(category_type)s::text IS NULL OR c.category_type = %(category_type)s::text)
  AND (%(min_size)s::int IS NULL OR c.size >= %(min_size)s::int)
  AND (%(max_size)s::int IS NULL OR c.size <= %(max_size)s::int)
  AND (%(min_score)s::real IS NULL OR c.min_edge_score >= %(min_score)s::real)
  AND (%(max_score)s::real IS NULL OR c.min_edge_score <= %(max_score)s::real)
  AND (%(shared_photo)s::boolean IS NULL
       OR c.shared_photo_warning = %(shared_photo)s::boolean)
  AND (%(has_judgement)s::boolean IS NULL
       OR (c.n_judged_edges > 0) = %(has_judgement)s::boolean)
  AND (%(verdict)s::text IS NULL
       OR (%(verdict)s::text = 'unreviewed' AND v.verdict IS NULL)
       OR v.verdict = %(verdict)s::text)
"""

# Default sort: the WEAKEST accepted edge first, because that is where the errors live (§8).
# `min_edge_score` is nullable (a singleton cluster has no edge), so the sort key coalesces to
# -1 and the cursor compares the same expression — an uncoalesced NULL would sort with the
# nulls-last default and then never satisfy the strict row-value comparison, wedging the page.
GROUPS_WEAKEST_SQL = (
    _CLUSTER_SELECT
    + _CLUSTER_WHERE
    + """
  AND (%(after_score)s::real IS NULL
       OR (coalesce(c.min_edge_score, -1::real), c.cluster_key)
          > (%(after_score)s::real, %(after_key)s::bigint))
ORDER BY coalesce(c.min_edge_score, -1::real) ASC, c.cluster_key ASC
LIMIT %(limit)s::int
"""
)

GROUPS_NEWEST_SQL = (
    _CLUSTER_SELECT
    + _CLUSTER_WHERE
    + """
  AND (%(after_ts)s::timestamptz IS NULL
       OR (c.last_changed_at, c.cluster_key)
          < (%(after_ts)s::timestamptz, %(after_key)s::bigint))
ORDER BY c.last_changed_at DESC, c.cluster_key DESC
LIMIT %(limit)s::int
"""
)

GROUPS_LARGEST_SQL = (
    _CLUSTER_SELECT
    + _CLUSTER_WHERE
    + """
  AND (%(after_size)s::int IS NULL
       OR (c.size, c.cluster_key) < (%(after_size)s::int, %(after_key)s::bigint))
ORDER BY c.size DESC, c.cluster_key DESC
LIMIT %(limit)s::int
"""
)

# THE UNBIASED SAMPLE ORDER (D6, §9). Every other sort here is a working order — weakest edge
# first puts the errors on top, which is what a review session wants and exactly what an ERROR
# RATE may not be measured on. A seeded hash of the key is a total order that correlates with
# nothing the engine did: `md5(cluster_key || seed)` is deterministic, so the same seed serves
# the same sample to the same operator across pages, reloads and days, and a NEW seed draws a
# fresh sample without a schema of its own.
#
# The cursor is the ORDER KEY, as everywhere else on this page: `(md5(…), cluster_key)`, with
# the hash recomputed by the statement rather than trusted from the wire (the route validates
# it as 32 hex characters, so an edited cursor is a 400 and never a predicate). Both sides of
# the comparison are the same expression under the same collation, which is what makes the
# page boundary exact instead of approximately right.
GROUPS_RANDOM_SQL = (
    _CLUSTER_SELECT
    + _CLUSTER_WHERE
    + """
  AND (%(after_hash)s::text IS NULL
       OR (md5(c.cluster_key::text || %(seed)s::text), c.cluster_key)
          > (%(after_hash)s::text, %(after_key)s::bigint))
ORDER BY md5(c.cluster_key::text || %(seed)s::text) ASC, c.cluster_key ASC
LIMIT %(limit)s::int
"""
)

# "20 of N", and N is the whole filtered set — the one number a keyset page cannot report
# about itself. Built from the SAME `_CLUSTER_WHERE` the page reads through, so the headline
# and the queue can never describe different cohorts; the route asks for it on the FIRST page
# only, because paging does not change it and counting again per page is pure cost.
GROUPS_COUNT_SQL = "SELECT count(*)" + _CLUSTER_FROM + _CLUSTER_WHERE

GROUP_ONE_SQL = (
    _CLUSTER_SELECT
    + """
WHERE c.cluster_key = %(cluster_key)s::bigint
  AND (%(generation)s::text IS NULL OR c.generation = %(generation)s::text)
"""
)

# ------------------------------------------------------------------------- members + photos

MEMBER_COLUMNS: tuple[str, ...] = (
    "cluster_key",
    "listing_id",
    "source",
    "source_url",
    "category_main",
    "category_type",
    "disposition",
    "area_m2",
    "floor",
    "price_czk",
    "first_seen_at",
    "last_seen_at",
    "is_active",
    "cover_storage_path",
    "cover_sreality_url",
    "n_images",
    "images",
)

# The cover is the FIRST image by gallery position — `sequence NULLS LAST, id` is the order
# every other reader of `images` in this repo uses, so the card and the carousel open on the
# same frame. A member whose listing row is gone (a shadow-mode cluster outlives nothing, but
# a listing can be pruned) still renders, hence the LEFT JOIN rather than an inner one.
# The QUEUE gallery: the first `card_frames` frames of each member, so a card can be paged
# rather than judged on one cover — the operator's own request. Ordered exactly like the cover
# above, which makes `images[0]` and `cover` the same frame by construction; `n_images` still
# reports the WHOLE album, so a card can say how many frames the dialog would add. Capped IN
# THE STATEMENT (the LISTING_IMAGES_SQL lesson): trimming after the fetch still drags a
# 120-frame album across the wire for every member of every cluster on the page.
GROUP_MEMBERS_SQL = """
SELECT
    m.cluster_key, m.listing_id,
    l.source, l.source_url, l.category_main, l.category_type, l.disposition,
    l.area_m2, l.floor, l.price_czk, l.first_seen_at, l.last_seen_at, l.is_active,
    cover.storage_path, cover.sreality_url, coalesce(gallery.n, 0),
    coalesce(frames.images, '[]'::json)
FROM autodedup.cluster_members m
LEFT JOIN listings l ON l.id = m.listing_id
LEFT JOIN LATERAL (
    SELECT i.storage_path, i.sreality_url
      FROM images i
     WHERE i.listing_id = m.listing_id
     ORDER BY i.sequence NULLS LAST, i.id
     LIMIT 1
) cover ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS n FROM images i WHERE i.listing_id = m.listing_id
) gallery ON true
LEFT JOIN LATERAL (
    SELECT json_agg(
               json_build_object(
                   'image_id', f.id,
                   'storage_path', f.storage_path,
                   'sreality_url', f.sreality_url,
                   'sequence', f.sequence
               )
               ORDER BY f.rn
           ) AS images
      FROM (
          SELECT i.id, i.storage_path, i.sreality_url, i.sequence,
                 row_number() OVER (ORDER BY i.sequence NULLS LAST, i.id) AS rn
            FROM images i
           WHERE i.listing_id = m.listing_id
           ORDER BY i.sequence NULLS LAST, i.id
           LIMIT %(card_frames)s::int
      ) f
) frames ON true
WHERE m.cluster_key = any(%(keys)s::bigint[])
ORDER BY m.cluster_key, m.listing_id
"""

# --------------------------------------------------------------- the members' own advert text

MEMBER_TEXT_COLUMNS: tuple[str, ...] = ("listing_id", "title", "description")

# THE DIALOG ONLY, never the queue. `GROUP_MEMBERS_SQL` runs for every member of every card on
# a 20-group page; `listings.description` is a TOASTed column and `raw_json` a whole payload, so
# selecting either there would detoast hundreds of adverts to render a photo strip nobody has
# opened yet. This statement runs once, over the members of the ONE cluster being opened.
#
# The title is not a column: each portal parser files it in `raw_json` under its own key
# (`title` for the six HTML portals, `advert_name` for sreality's v1 API; `name` is the generic
# fallback). `nullif(btrim(...), '')` so an empty string is an absent title, not a blank heading.
# Both fields go through `autodedup.judge.scrubbed_text` before they reach a response (E28) —
# the statement selects no broker column, and a bazos advert signs its title as often as its body.
MEMBER_TEXT_SQL = """
SELECT
    l.id,
    coalesce(
        nullif(btrim(l.raw_json->>'title'), ''),
        nullif(btrim(l.raw_json->>'advert_name'), ''),
        nullif(btrim(l.raw_json->>'name'), '')
    ),
    l.description
FROM listings l
WHERE l.id = any(%(ids)s::bigint[])
ORDER BY l.id
"""

IMAGE_COLUMNS: tuple[str, ...] = (
    "listing_id",
    "image_id",
    "sequence",
    "storage_path",
    "sreality_url",
    "phash",
)

# Bounded IN THE STATEMENT, not in Python: a 30-frame cap that only trims after the fetch
# still drags every frame of a 120-image listing across the wire for each member.
#
# `phash::text`: a dHash is a full 64-bit integer and JSON has no such number — anything at
# or above 2^53 is rounded by the browser's parser, which is most of them. The digit string
# is the honest wire shape (and the one the SPA client types), and the ONE place the value is
# load-bearing (the Hamming distance below) reads it back as an int server-side.
LISTING_IMAGES_SQL = """
SELECT listing_id, image_id, sequence, storage_path, sreality_url, phash
FROM (
    SELECT
        i.listing_id, i.id AS image_id, i.sequence, i.storage_path, i.sreality_url,
        i.phash::text AS phash,
        row_number() OVER (
            PARTITION BY i.listing_id ORDER BY i.sequence NULLS LAST, i.id
        ) AS rn
      FROM images i
     WHERE i.listing_id = any(%(ids)s::bigint[])
) ranked
WHERE rn <= %(per_listing)s::int
ORDER BY listing_id, rn
"""

LISTING_PHASH_COLUMNS: tuple[str, ...] = ("listing_id", "image_id", "phash")

# The pair view's Hamming distances run over EVERY frame, not over the 30 the gallery renders:
# the engine scored the photo family across the whole album, so a nearest-frame search that
# stopped at 30 would answer "no match" for evidence the IMG family bit on the same page was
# computed from. Only the three columns the distance needs travel.
LISTING_PHASHES_SQL = """
SELECT i.listing_id, i.id AS image_id, i.phash
FROM images i
WHERE i.listing_id = any(%(ids)s::bigint[])
  AND i.phash IS NOT NULL
ORDER BY i.listing_id, i.sequence NULLS LAST, i.id
"""

# ------------------------------------------------------------------------------ pair grain

PAIR_COLUMNS: tuple[str, ...] = (
    "listing_lo",
    "listing_hi",
    "probes",
    "families",
    "features",
    "score",
    "zone",
    "decision",
    "guard_veto",
    "cluster_key",
    "feature_version",
    "model_version",
    "decided_at",
)

_PAIR_SELECT_LIST = """
    p.listing_lo, p.listing_hi, p.probes, p.families, p.features, p.score, p.zone,
    p.decision, p.guard_veto, p.cluster_key, p.feature_version, p.model_version, p.decided_at
"""

CLUSTER_PAIRS_SQL = (
    "SELECT"
    + _PAIR_SELECT_LIST
    + """
FROM autodedup.pairs p
WHERE p.listing_lo = any(%(ids)s::bigint[])
  AND p.listing_hi = any(%(ids)s::bigint[])
ORDER BY p.score DESC NULLS LAST, p.listing_lo, p.listing_hi
"""
)

PAIR_ONE_SQL = (
    "SELECT"
    + _PAIR_SELECT_LIST
    + """
FROM autodedup.pairs p
WHERE p.listing_lo = %(listing_lo)s::bigint AND p.listing_hi = %(listing_hi)s::bigint
"""
)

EDGE_SUMMARY_COLUMNS: tuple[str, ...] = (
    "cluster_key",
    "n_edges",
    "min_score",
    "mean_score",
    "n_certificates",
    "n_judged",
    "families",
)

# An edge OF a cluster is one whose BOTH listings are members of it — the same question
# `CLUSTER_PAIRS_SQL` asks, so the card's chips and the detail view's pair table cannot
# disagree. Keying on `pairs.cluster_key` instead would miss every edge stored with a null
# key whose two listings were pulled into one cluster by other edges, and the card would
# then under-count against `clusters.min_edge_score` / `n_certificate_edges` beside it.
# `EXISTS` cannot ride inside an aggregate's FILTER, so the judged-edge count comes off a
# LATERAL count instead. `bit_or` over the per-pair family bitmask is the cluster's union of
# evidence families, which is what the evidence chips read.
EDGE_SUMMARY_SQL = """
SELECT
    ma.cluster_key,
    count(*)                                                                   AS n_edges,
    min(p.score)                                                               AS min_score,
    avg(p.score)                                                               AS mean_score,
    count(*) FILTER (WHERE split_part(p.decision, ':', 1) = 'certificate')     AS n_certificates,
    count(*) FILTER (WHERE judged.n > 0)                                       AS n_judged,
    coalesce(bit_or(p.families), 0::smallint)                                  AS families
FROM autodedup.cluster_members ma
JOIN autodedup.cluster_members mb ON mb.cluster_key = ma.cluster_key
JOIN autodedup.pairs p
  ON p.listing_lo = ma.listing_id AND p.listing_hi = mb.listing_id
LEFT JOIN LATERAL (
    SELECT count(*) AS n
      FROM autodedup.judgements j
     WHERE j.listing_lo = p.listing_lo AND j.listing_hi = p.listing_hi
) judged ON true
WHERE ma.cluster_key = any(%(keys)s::bigint[])
GROUP BY ma.cluster_key
"""

JUDGEMENT_COLUMNS: tuple[str, ...] = (
    "listing_lo",
    "listing_hi",
    "judge_version",
    "tier",
    "model",
    "verdict",
    "confidence",
    "unit_discriminator",
    "key_evidence",
    "contradicting_evidence",
    "developer_project_suspected",
    "cost_usd",
    "created_at",
)

# LATEST per (pair, tier): a re-run at a newer `judge_version` is a new opinion, and the UI
# shows the current one per tier rather than every historical version stacked.
# `llm_call_id` is deliberately absent — it is the lane's cost join key, not operator evidence.
JUDGEMENTS_LATEST_SQL = """
SELECT DISTINCT ON (j.listing_lo, j.listing_hi, j.tier)
    j.listing_lo, j.listing_hi, j.judge_version, j.tier, j.model, j.verdict, j.confidence,
    j.unit_discriminator, j.key_evidence, j.contradicting_evidence,
    j.developer_project_suspected, j.cost_usd, j.created_at
FROM autodedup.judgements j
WHERE (j.listing_lo, j.listing_hi) IN (
    SELECT lo, hi FROM unnest(%(los)s::bigint[], %(his)s::bigint[]) AS pair(lo, hi)
)
ORDER BY j.listing_lo, j.listing_hi, j.tier, j.created_at DESC
"""

VERDICT_COLUMNS: tuple[str, ...] = (
    "id",
    "kind",
    "cluster_key",
    "listing_lo",
    "listing_hi",
    "verdict",
    "weight",
    "note",
    # Migration 533. The structured half of "why" — `note` is the prose half, and the two are
    # written and read together everywhere a verdict is.
    "reasons",
    "decided_by",
    "decided_at",
)

_VERDICT_SELECT_LIST = """
    v.id, v.kind, v.cluster_key, v.listing_lo, v.listing_hi, v.verdict, v.weight, v.note,
    v.reasons, v.decided_by, v.decided_at
"""

PAIR_VERDICTS_SQL = (
    "SELECT"
    + _VERDICT_SELECT_LIST
    + """
FROM autodedup.verdicts v
WHERE v.kind = 'pair'
  AND (v.listing_lo, v.listing_hi) IN (
      SELECT lo, hi FROM unnest(%(los)s::bigint[], %(his)s::bigint[]) AS pair(lo, hi)
  )
ORDER BY v.decided_at DESC, v.id DESC
"""
)

# Every operator verdict on a pair of listings drawn from ONE set — the members of a cluster.
# `PAIR_VERDICTS_SQL` keys on the pairs the engine SCORED, which is the wrong question here: a
# split rules on every member pair, including the ones that carry no edge at all, so a queue
# card rehydrating its assignment from the store would miss exactly the pairs the operator
# separated by hand.
MEMBER_PAIR_VERDICTS_SQL = (
    "SELECT"
    + _VERDICT_SELECT_LIST
    + """
FROM autodedup.verdicts v
WHERE v.kind = 'pair'
  AND v.listing_lo = any(%(ids)s::bigint[])
  AND v.listing_hi = any(%(ids)s::bigint[])
ORDER BY v.decided_at DESC, v.id DESC
"""
)

CLUSTER_VERDICTS_SQL = (
    "SELECT"
    + _VERDICT_SELECT_LIST
    + """
FROM autodedup.verdicts v
WHERE v.kind = 'cluster' AND v.cluster_key = %(cluster_key)s::bigint
ORDER BY v.decided_at DESC, v.id DESC
"""
)

CONFLICT_COLUMNS: tuple[str, ...] = (
    "id",
    "kind",
    "cluster_key_a",
    "cluster_key_b",
    "listing_lo",
    "listing_hi",
    "invariant",
    "detail",
    "created_at",
)

# A conflict TOUCHES a cluster either by naming it or by naming one of its members — the
# second arm is what surfaces the union an invariant refused, whose row carries the two
# listings and no cluster key at all.
CLUSTER_CONFLICTS_SQL = """
SELECT
    cc.id, cc.kind, cc.cluster_key_a, cc.cluster_key_b, cc.listing_lo, cc.listing_hi,
    cc.invariant, cc.detail, cc.created_at
FROM autodedup.cluster_conflicts cc
WHERE cc.cluster_key_a = %(cluster_key)s::bigint
   OR cc.cluster_key_b = %(cluster_key)s::bigint
   OR cc.listing_lo = any(%(ids)s::bigint[])
   OR cc.listing_hi = any(%(ids)s::bigint[])
ORDER BY cc.created_at DESC, cc.id DESC
"""

# ------------------------------------------------------------------------- the residual view

RESIDUAL_COLUMNS: tuple[str, ...] = (
    "listing_lo",
    "listing_hi",
    "score",
    "zone",
    "decision",
    "guard_veto",
    "families",
    "probes",
    "features",
    "feature_version",
    "model_version",
    "decided_at",
    "block_key",
    "a_source",
    "a_source_url",
    "a_category_main",
    "a_category_type",
    "a_disposition",
    "a_area_m2",
    "a_floor",
    "a_total_floors",
    "a_price_czk",
    "a_first_seen_at",
    "a_last_seen_at",
    "a_is_active",
    "a_cover_storage_path",
    "a_cover_sreality_url",
    "a_n_images",
    "b_source",
    "b_source_url",
    "b_category_main",
    "b_category_type",
    "b_disposition",
    "b_area_m2",
    "b_floor",
    "b_total_floors",
    "b_price_czk",
    "b_first_seen_at",
    "b_last_seen_at",
    "b_is_active",
    "b_cover_storage_path",
    "b_cover_sreality_url",
    "b_n_images",
    "judge_verdict",
    "judge_confidence",
    "judge_tier",
    "verdict",
    "verdict_note",
    "verdict_reasons",
    "verdict_decided_by",
    "verdict_decided_at",
)

# "Residual" = scored above the display floor and NOT joined into one cluster of this
# generation. The `NOT EXISTS` asks exactly that question of `cluster_members` rather than
# trusting `pairs.cluster_key`: an edge can be stored with a null cluster key and still have
# both its listings pulled into one cluster by other edges, and that pair is not a residual.
#
# Split into FROM / covers / WHERE for the same reason the cluster statement is: the count
# behind "20 of N" reuses the filter text verbatim, and joins ONLY what the filters read.
# The four cover/gallery LATERALs are display, so the count never runs them.
_RESIDUAL_FROM = """
FROM autodedup.pairs p
JOIN listings la ON la.id = p.listing_lo
JOIN listings lb ON lb.id = p.listing_hi
LEFT JOIN LATERAL (
    SELECT jj.verdict, jj.confidence, jj.tier
      FROM autodedup.judgements jj
     WHERE jj.listing_lo = p.listing_lo AND jj.listing_hi = p.listing_hi
     -- AUTHORITY, not recency. `oss` is the rented open-model ARM: it answers the same pairs
     -- gold already answered, so on `created_at` alone an experimental 7B verdict would
     -- silently replace ground truth as the pair's headline. Rank the tiers, and only fall
     -- back to the clock within one of them.
     ORDER BY CASE jj.tier WHEN 'gold' THEN 0 WHEN 'vision' THEN 1 WHEN 'text' THEN 2
                           ELSE 3 END,
              jj.created_at DESC
     LIMIT 1
) j ON true
LEFT JOIN LATERAL (
    SELECT vv.verdict, vv.note, vv.reasons, vv.decided_by, vv.decided_at
      FROM autodedup.verdicts vv
     WHERE vv.kind = 'pair'
       AND vv.listing_lo = p.listing_lo AND vv.listing_hi = p.listing_hi
     ORDER BY vv.decided_at DESC, vv.id DESC
     LIMIT 1
) v ON true
"""

# The block a residual pair sits in, for the row's own label and for the BLOCK filter.
#
# READ FROM `listing_location`, NOT `autodedup.listing_fp`. The engine's block key IS a
# location fact: `fingerprint.block_key_of` is `cast_obce_kod` when the town is split, else
# `obec_kod`, and this LATERAL spells that same rule. `listing_fp` is the lane's own scratch
# copy of it and NO shipped lane writes a row into it, so the predicate that read it matched
# nothing for every block an operator could pick — a filter that silently empties the queue,
# which is the defect the named picker exists to remove. `listing_location_pkey` is unique on
# `listing_id`, so this is a primary-key lookup and cannot multiply a pair into two rows.
_RESIDUAL_BLOCK = """
LEFT JOIN LATERAL (
    SELECT coalesce(ll.cast_obce_kod, ll.obec_kod) AS block_key,
           CASE WHEN ll.cast_obce_kod IS NOT NULL THEN 'c'
                WHEN ll.obec_kod IS NOT NULL THEN 'o' END AS block_grain
      FROM listing_location ll
     WHERE ll.listing_id = p.listing_lo
) bl ON true
"""

_RESIDUAL_COVERS = """
LEFT JOIN LATERAL (
    SELECT i.storage_path, i.sreality_url FROM images i
     WHERE i.listing_id = p.listing_lo ORDER BY i.sequence NULLS LAST, i.id LIMIT 1
) ca ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS n FROM images i WHERE i.listing_id = p.listing_lo
) ga ON true
LEFT JOIN LATERAL (
    SELECT i.storage_path, i.sreality_url FROM images i
     WHERE i.listing_id = p.listing_hi ORDER BY i.sequence NULLS LAST, i.id LIMIT 1
) cb ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS n FROM images i WHERE i.listing_id = p.listing_hi
) gb ON true
"""

_RESIDUAL_FILTERS = """
WHERE p.score >= %(min_score)s::real
  AND (%(zone)s::text IS NULL OR p.zone = %(zone)s::text)
  AND (%(block)s::bigint IS NULL
       OR (bl.block_key = %(block)s::bigint
           AND (%(block_grain)s::text IS NULL
                OR bl.block_grain = %(block_grain)s::text)))
  AND (%(source_pair)s::text IS NULL
       OR least(la.source, lb.source) || '+' || greatest(la.source, lb.source)
          = %(source_pair)s::text)
  AND (%(has_judgement)s::boolean IS NULL
       OR (j.verdict IS NOT NULL) = %(has_judgement)s::boolean)
  AND (%(verdict)s::text IS NULL
       OR (%(verdict)s::text = 'unreviewed' AND v.verdict IS NULL)
       OR v.verdict = %(verdict)s::text)
"""

# WHAT MAKES A PAIR RESIDUAL, in one place: the two listings are not in ONE cluster of this
# generation. Its own constant because the validation-progress sample below asks the identical
# question — a second spelling of it would let the counter count a cohort the queue does not show.
_RESIDUAL_UNCLUSTERED = """  AND NOT EXISTS (
      SELECT 1
        FROM autodedup.cluster_members ma
        JOIN autodedup.cluster_members mb
          ON mb.cluster_key = ma.cluster_key AND mb.listing_id = p.listing_hi
        JOIN autodedup.clusters cl
          ON cl.cluster_key = ma.cluster_key AND cl.generation = %(generation)s::text
       WHERE ma.listing_id = p.listing_lo
  )
"""

_RESIDUAL_WHERE = _RESIDUAL_FILTERS + _RESIDUAL_UNCLUSTERED

_RESIDUAL_SELECT = """
SELECT
    p.listing_lo, p.listing_hi, p.score, p.zone, p.decision, p.guard_veto, p.families,
    p.probes, p.features, p.feature_version, p.model_version, p.decided_at,
    bl.block_key,
    la.source, la.source_url, la.category_main, la.category_type, la.disposition,
    la.area_m2, la.floor, la.total_floors, la.price_czk, la.first_seen_at, la.last_seen_at,
    la.is_active, ca.storage_path, ca.sreality_url, coalesce(ga.n, 0),
    lb.source, lb.source_url, lb.category_main, lb.category_type, lb.disposition,
    lb.area_m2, lb.floor, lb.total_floors, lb.price_czk, lb.first_seen_at, lb.last_seen_at,
    lb.is_active, cb.storage_path, cb.sreality_url, coalesce(gb.n, 0),
    j.verdict, j.confidence, j.tier,
    v.verdict, v.note, v.reasons, v.decided_by, v.decided_at
"""

RESIDUAL_SQL = (
    _RESIDUAL_SELECT
    + _RESIDUAL_FROM
    + _RESIDUAL_BLOCK
    + _RESIDUAL_COVERS
    + _RESIDUAL_WHERE
    + """
  AND (%(after_score)s::real IS NULL
       OR (p.score, p.listing_lo, p.listing_hi)
          < (%(after_score)s::real, %(after_lo)s::bigint, %(after_hi)s::bigint))
ORDER BY p.score DESC, p.listing_lo DESC, p.listing_hi DESC
LIMIT %(limit)s::int
"""
)

# The residual view's own unbiased order — the same device as `GROUPS_RANDOM_SQL`, keyed on the
# PAIR (`lo:hi`, with a separator so `1:23` and `12:3` are different strings rather than one).
# Score-descending is the working order and puts the likeliest duplicates on top; an agreement
# number measured on it would be an agreement number about the top of the queue.
RESIDUAL_RANDOM_SQL = (
    _RESIDUAL_SELECT
    + _RESIDUAL_FROM
    + _RESIDUAL_BLOCK
    + _RESIDUAL_COVERS
    + _RESIDUAL_WHERE
    + """
  AND (%(after_hash)s::text IS NULL
       OR (md5(p.listing_lo::text || ':' || p.listing_hi::text || %(seed)s::text),
           p.listing_lo, p.listing_hi)
          > (%(after_hash)s::text, %(after_lo)s::bigint, %(after_hi)s::bigint))
ORDER BY md5(p.listing_lo::text || ':' || p.listing_hi::text || %(seed)s::text) ASC,
         p.listing_lo ASC, p.listing_hi ASC
LIMIT %(limit)s::int
"""
)

# The residual half of "20 of N". Same FROM minus the four display LATERALs, same WHERE minus
# the cursor — the cursor is where the page is, not what the filter selects. The block
# LATERAL is NOT display and stays: the WHERE reads it, and a count that dropped it would
# answer a different question from the list above it.
RESIDUAL_COUNT_SQL = "SELECT count(*)" + _RESIDUAL_FROM + _RESIDUAL_BLOCK + _RESIDUAL_WHERE

# ---------------------------------------------------------------- the blocks of a generation

BLOCK_COLUMNS: tuple[str, ...] = (
    "block_key",
    "block_grain",
    "name",
    "n_clusters",
    "n_listings",
)

# What the BLOCK filter offers instead of a free-text numeric field: every block this
# generation actually clustered, with the town/quarter NAME an operator recognises.
#
# The name is not a column of this schema. `listing_location` (migration 501) is the one
# store of resolved admin names, and the key a cluster carries is a RÚIAN code at one of two
# grains (migration 529: `c` = část obce, `o` = obec). So the name is the MOST FREQUENT
# spelling among the located listings whose code equals the key — most frequent rather than
# any, because two neighbouring rows can disagree while the resolver is mid-flight, and a
# picker whose label flickers between revisions is worse than one that lags one of them.
#
# Bounded to the keys this generation uses: the name aggregate is a semi-join against the
# block list, never a group-by over the whole location table.
#
# BOUNDED IN LENGTH TOO. `listing_location` carries no index on `cast_obce_kod`, so the
# quarter arm is a scan; and at corpus scale the vocabulary is thousands of obce, which is a
# payload nobody reads and a native select nobody can use. The busiest blocks first, capped
# by the caller — a block outside the cap still filters, because the picker keeps whatever
# key the URL arrived with.
#
# NO PAIR COUNT. `autodedup.pairs` carries no block column, and the only per-listing block
# store (`autodedup.listing_fp`) is written by no shipped lane — a "pairs in this block"
# number read off it would be a confident zero. The counts here are what the clusters
# themselves say: how many groups sit in the block, and how many adverts those groups hold.
BLOCKS_SQL = """
WITH blocks AS (
    SELECT c.block_key,
           c.block_grain,
           count(*)::bigint                  AS n_clusters,
           coalesce(sum(c.size), 0)::bigint  AS n_listings
      FROM autodedup.clusters c
     WHERE c.generation = %(generation)s::text
       AND c.block_key IS NOT NULL
     GROUP BY c.block_key, c.block_grain
),
named AS (
    -- `n` rides in the select list because DISTINCT ON is fussy about ordering by a column
    -- it cannot see; the outer query reads the name only.
    SELECT DISTINCT ON (grain, kod) grain, kod, name, n
      FROM (
          SELECT 'o'::text AS grain, l.obec_kod AS kod, l.obec_name AS name, count(*) AS n
            FROM listing_location l
           WHERE l.obec_name IS NOT NULL
             AND l.obec_kod IN (SELECT b.block_key FROM blocks b WHERE b.block_grain = 'o')
           GROUP BY 1, 2, 3
          UNION ALL
          SELECT 'c'::text, l.cast_obce_kod, l.cast_obce_name, count(*)
            FROM listing_location l
           WHERE l.cast_obce_name IS NOT NULL
             AND l.cast_obce_kod IN (SELECT b.block_key FROM blocks b WHERE b.block_grain = 'c')
           GROUP BY 1, 2, 3
      ) counted
     ORDER BY grain, kod, n DESC, name ASC
)
SELECT b.block_key, b.block_grain, nm.name, b.n_clusters, b.n_listings
  FROM blocks b
  LEFT JOIN named nm ON nm.grain = b.block_grain AND nm.kod = b.block_key
 ORDER BY b.n_clusters DESC, b.block_key ASC
 LIMIT %(limit)s::int
"""

# The two grains a block can be keyed at (migration 529), in ONE place: the route validates an
# arriving `block_grain` against it, and both list statements filter on it.
BLOCK_GRAIN_VALUES: tuple[str, ...] = ("o", "c")

# ----------------------------------------------------------- the pair view's listing digests

# The digest side of `GET /autodedup/pair`. Mirrors `export_sql.COHORT_LISTINGS_SQL` MINUS
# every broker column: the export needs `broker_phone`/`broker_email` to salt its broker key,
# this read needs neither, so they are not selected at all (E28).
LISTING_DETAIL_COLUMNS: tuple[str, ...] = (
    "id",
    "source",
    "source_id_native",
    "source_url",
    "category_main",
    "category_type",
    "subtype",
    "disposition",
    "area_m2",
    "floor",
    "total_floors",
    "price_czk",
    "price_unit",
    "area_basis",
    "has_balcony",
    "has_parking",
    "has_lift",
    "building_type",
    "condition",
    "energy_rating",
    "estate_area",
    "usable_area",
    "garden_area",
    "category_sub_cb",
    "furnished",
    "terrace",
    "cellar",
    "garage",
    "parking_lots",
    "ownership",
    "published_at",
    "description",
    "first_seen_at",
    "last_seen_at",
    "inactive_at",
    "is_active",
)

LISTING_DETAIL_SQL = """
SELECT
    l.id, l.source, l.source_id_native, l.source_url, l.category_main, l.category_type,
    l.subtype, l.disposition, l.area_m2, l.floor, l.total_floors, l.price_czk, l.price_unit,
    l.area_basis, l.has_balcony, l.has_parking, l.has_lift, l.building_type, l.condition,
    l.energy_rating, l.estate_area, l.usable_area, l.garden_area, l.category_sub_cb,
    l.furnished, l.terrace, l.cellar, l.garage, l.parking_lots, l.ownership, l.published_at,
    l.description, l.first_seen_at, l.last_seen_at, l.inactive_at, l.is_active
FROM listings l
WHERE l.id = any(%(ids)s::bigint[])
ORDER BY l.id
"""

# --------------------------------------------------------------------- the engine stat strip

ZONE_COUNT_COLUMNS: tuple[str, ...] = ("zone", "n")

PAIR_ZONES_SQL = """
SELECT coalesce(p.zone, 'unscored') AS zone, count(*) AS n
FROM autodedup.pairs p
GROUP BY 1
ORDER BY 1
"""

CERTIFICATE_COUNT_COLUMNS: tuple[str, ...] = ("certificate", "n")

CERTIFICATE_COUNTS_SQL = """
SELECT split_part(p.decision, ':', 2) AS certificate, count(*) AS n
FROM autodedup.pairs p
WHERE split_part(p.decision, ':', 1) = 'certificate'
GROUP BY 1
ORDER BY 1
"""

GENERATION_COLUMNS: tuple[str, ...] = (
    "generation",
    "n_clusters",
    "n_members",
    "n_conflicted",
    "last_changed_at",
)

GENERATION_COUNTS_SQL = """
SELECT
    c.generation,
    count(*)                                          AS n_clusters,
    coalesce(sum(c.size), 0)                          AS n_members,
    count(*) FILTER (WHERE c.status = 'conflict')     AS n_conflicted,
    max(c.last_changed_at)                            AS last_changed_at
FROM autodedup.clusters c
GROUP BY c.generation
ORDER BY max(c.last_changed_at) DESC
"""

# WHICH pass a validation view reads when the caller names none. The first row of
# GENERATION_COUNTS_SQL by construction — the generation whose clusters changed most recently —
# because the list the picker offers and the default the queue opens on must never disagree
# about which pass is current. A hard-coded default is what put a superseded generation's
# proposals in front of the operator; the store names the newest pass, so the store is asked.
LATEST_GENERATION_SQL = """
SELECT c.generation
FROM autodedup.clusters c
ORDER BY c.last_changed_at DESC
LIMIT 1
"""

VERDICT_COUNT_COLUMNS: tuple[str, ...] = ("kind", "verdict", "n")

VERDICT_COUNTS_SQL = """
SELECT v.kind, v.verdict, count(*) AS n
FROM autodedup.verdicts v
GROUP BY 1, 2
ORDER BY 1, 2
"""

# The reason histogram (migration 533): WHAT the operator saw, counted per grain. Pair and
# cluster stay separate columns on the page because they answer different questions — a chip
# on a pair names the discriminator one edge missed, the same chip on a cluster names why a
# whole proposal was wrong — and summing them would hide both. `unnest` is a LATERAL over the
# array, so a verdict with no reason contributes no row at all rather than a null bucket.
REASON_COUNT_COLUMNS: tuple[str, ...] = ("kind", "reason", "n")

REASON_COUNTS_SQL = """
SELECT v.kind, r.reason, count(*) AS n
FROM autodedup.verdicts v
CROSS JOIN LATERAL unnest(v.reasons) AS r(reason)
GROUP BY 1, 2
ORDER BY 1, 2
"""

JUDGEMENT_COUNT_COLUMNS: tuple[str, ...] = ("tier", "verdict", "n")

JUDGEMENT_COUNTS_SQL = """
SELECT j.tier, j.verdict, count(*) AS n
FROM autodedup.judgements j
GROUP BY 1, 2
ORDER BY 1, 2
"""

SCORE_RUN_COLUMNS: tuple[str, ...] = (
    "id",
    "status",
    "fingerprint",
    "cohort",
    "params",
    "stats",
    "started_at",
    "finished_at",
)

LAST_SCORE_RUN_SQL = """
SELECT r.id, r.status, r.fingerprint, r.cohort, r.params, r.stats, r.started_at, r.finished_at
FROM autodedup.runs r
WHERE r.mode = %(mode)s::text
ORDER BY r.started_at DESC
LIMIT 1
"""

# -------------------------------------------------------------------------- operator writes

# Re-deciding UPSERTs rather than stacking (migration 528's two partial unique indexes). The
# `WHERE kind = …` on the conflict target is how a PARTIAL unique index is inferred — without
# it Postgres cannot match the index and raises 42P10.
VERDICT_PAIR_UPSERT_SQL = """
INSERT INTO autodedup.verdicts (kind, listing_lo, listing_hi, verdict, note, reasons,
                                decided_by)
VALUES ('pair', %(listing_lo)s::bigint, %(listing_hi)s::bigint, %(verdict)s::text,
        %(note)s::text, %(reasons)s::text[], %(decided_by)s::text)
ON CONFLICT (kind, listing_lo, listing_hi, decided_by) WHERE kind = 'pair'
DO UPDATE SET verdict = excluded.verdict, note = excluded.note,
              reasons = excluded.reasons, decided_at = now()
RETURNING id, kind, cluster_key, listing_lo, listing_hi, verdict, weight, note, reasons,
          decided_by, decided_at
"""

VERDICT_CLUSTER_UPSERT_SQL = """
INSERT INTO autodedup.verdicts (kind, cluster_key, verdict, note, reasons, decided_by)
VALUES ('cluster', %(cluster_key)s::bigint, %(verdict)s::text, %(note)s::text,
        %(reasons)s::text[], %(decided_by)s::text)
ON CONFLICT (kind, cluster_key, decided_by) WHERE kind = 'cluster'
DO UPDATE SET verdict = excluded.verdict, note = excluded.note,
              reasons = excluded.reasons, decided_at = now()
RETURNING id, kind, cluster_key, listing_lo, listing_hi, verdict, weight, note, reasons,
          decided_by, decided_at
"""

# PERMANENT (§13's own word): an operator "not the same" outranks every machine source, so it
# overwrites a `guard`/`model`/`llm` row rather than losing the conflict.
MUST_NOT_LINK_UPSERT_SQL = """
INSERT INTO autodedup.must_not_link (listing_lo, listing_hi, source, reason)
VALUES (%(listing_lo)s::bigint, %(listing_hi)s::bigint, 'operator', %(reason)s::text)
ON CONFLICT (listing_lo, listing_hi)
DO UPDATE SET source = 'operator', reason = excluded.reason, created_at = now()
"""

# The other half of the loop. A must-not-link is permanent against every MACHINE source, but
# the operator who wrote it must be able to take it back: re-deciding the same pair as
# `same`/`unsure` drops the row, or the engine would keep vetoing a pair the operator has
# since confirmed while the UI showed the corrected verdict. Only the operator's own row is
# dropped — a `guard`/`model`/`llm` veto is not the operator's to retract.
MUST_NOT_LINK_RETRACT_SQL = """
DELETE FROM autodedup.must_not_link
WHERE listing_lo = %(listing_lo)s::bigint
  AND listing_hi = %(listing_hi)s::bigint
  AND source = 'operator'
"""

# Just the ids — the membership a whole-cluster ruling fans out over. `GROUP_MEMBERS_SQL`
# answers the same question with three lateral joins and a gallery, which a write does not need.
CLUSTER_MEMBER_IDS_SQL = """
SELECT m.listing_id
FROM autodedup.cluster_members m
WHERE m.cluster_key = %(cluster_key)s::bigint
ORDER BY m.listing_id
"""

CLUSTER_EXISTS_SQL = """
SELECT 1 FROM autodedup.clusters WHERE cluster_key = %(cluster_key)s::bigint
"""

PAIR_EXISTS_SQL = """
SELECT 1 FROM autodedup.pairs
WHERE listing_lo = %(listing_lo)s::bigint AND listing_hi = %(listing_hi)s::bigint
"""

# ------------------------------------------------- the validation session (D6): how far in?
#
# "How many do I need to do?" is the question these four statements answer, and the honest
# answer has two halves. The WHOLE-GENERATION half says how much of the queue carries a ruling
# at all. The SAMPLE half says how far through the first N of the seeded random order the
# operator is — which is the number that matters, because an error rate measured on the
# weakest-edge order is an error rate about the weakest edges, not about the engine.
#
# THE SAMPLE IS DELIBERATELY UNFILTERED (beyond the generation, and the residual view's display
# floor). A sample that moved with the filter bar would be a different sample per page, and
# "37 of 100" would mean nothing across two of them. The page says so in words next to it.

VALIDATION_COUNT_COLUMNS: tuple[str, ...] = ("n", "n_reviewed", "n_not_same")

# The verdict LATERAL is the SAME "latest wins" read the queue's own statement makes (one row
# per cluster, newest first) — a count that summed `autodedup.verdicts` directly would count a
# re-decided group twice and report more reviewed groups than the generation holds.
_CLUSTER_VERDICT_LATERAL = """
LEFT JOIN LATERAL (
    SELECT vv.verdict
      FROM autodedup.verdicts vv
     WHERE vv.kind = 'cluster' AND vv.cluster_key = s.cluster_key
     ORDER BY vv.decided_at DESC, vv.id DESC
     LIMIT 1
) v ON true
"""

_PAIR_VERDICT_LATERAL = """
LEFT JOIN LATERAL (
    SELECT vv.verdict
      FROM autodedup.verdicts vv
     WHERE vv.kind = 'pair'
       AND vv.listing_lo = s.listing_lo AND vv.listing_hi = s.listing_hi
     ORDER BY vv.decided_at DESC, vv.id DESC
     LIMIT 1
) v ON true
"""

_VALIDATION_COUNTS = """
SELECT
    count(*)                                                        AS n,
    count(v.verdict)                                                AS n_reviewed,
    count(*) FILTER (WHERE v.verdict IS NOT NULL AND v.verdict <> 'same') AS n_not_same
"""

VALIDATION_GROUPS_SAMPLE_SQL = (
    """
WITH s AS (
    SELECT c.cluster_key
      FROM autodedup.clusters c
     WHERE c.generation = %(generation)s::text
     ORDER BY md5(c.cluster_key::text || %(seed)s::text) ASC, c.cluster_key ASC
     LIMIT %(sample_size)s::int
)
"""
    + _VALIDATION_COUNTS
    + "FROM s"
    + _CLUSTER_VERDICT_LATERAL
)

VALIDATION_GROUPS_TOTAL_SQL = (
    """
WITH s AS (
    SELECT c.cluster_key FROM autodedup.clusters c
     WHERE c.generation = %(generation)s::text
)
"""
    + _VALIDATION_COUNTS
    + "FROM s"
    + _CLUSTER_VERDICT_LATERAL
)

VALIDATION_RESIDUAL_SAMPLE_SQL = (
    """
WITH s AS (
    SELECT p.listing_lo, p.listing_hi
      FROM autodedup.pairs p
     WHERE p.score >= %(min_score)s::real
"""
    + _RESIDUAL_UNCLUSTERED
    + """     ORDER BY md5(p.listing_lo::text || ':' || p.listing_hi::text || %(seed)s::text) ASC,
              p.listing_lo ASC, p.listing_hi ASC
     LIMIT %(sample_size)s::int
)
"""
    + _VALIDATION_COUNTS
    + "FROM s"
    + _PAIR_VERDICT_LATERAL
)

VALIDATION_RESIDUAL_TOTAL_SQL = (
    """
WITH s AS (
    SELECT p.listing_lo, p.listing_hi
      FROM autodedup.pairs p
     WHERE p.score >= %(min_score)s::real
"""
    + _RESIDUAL_UNCLUSTERED
    + """)
"""
    + _VALIDATION_COUNTS
    + "FROM s"
    + _PAIR_VERDICT_LATERAL
)

# ------------------------------------------ operator vs judge (D6: the gate, measured live)

AGREEMENT_COLUMNS: tuple[str, ...] = (
    "listing_lo",
    "listing_hi",
    "operator_verdict",
    "operator_source",
    "judge_verdict",
    "judge_tier",
    "judge_model",
)

# ONE statement, one row per COMPARABLE PAIR, and the arithmetic in Python (`autodedup.
# agreement`) — a Wilson interval written in SQL is a formula nobody can test by hand.
#
# THE OPERATOR'S LABEL SET IS TWO THINGS UNIONED. An explicit pair verdict is the obvious half.
# The other half is IMPLIED: confirming a cluster says every pair inside it is one property,
# which is exactly the statement the judge made per pair — and 103 confirmed groups carry far
# more pair-grade evidence than the handful of pairs ruled on one at a time. An explicit
# verdict WINS over an implied one (the operator looked at that pair), `unsure` is not a label
# at all, and only `same` clusters imply anything: a rejected group says the members are not
# ALL one property, never which pair inside it was the wrong one.
#
# THE EXPANSION IS BOUNDED. A cluster of n members implies n(n-1)/2 pairs, so a runaway group
# would dominate the number it is measured with. `max_cluster_size` caps which clusters expand
# at all (the route sends it, and reports how many clusters it skipped), so the cost is
# O(n_clusters x cap^2) rather than O(size^2) of the worst group.
#
# THE JUDGE'S LABEL IS THE BEST TIER, NOT THE NEWEST ROW: gold > vision > text, `oss` excluded
# entirely (a rented open-model arm answers the same pairs gold already answered — on a clock
# it would silently replace ground truth). `insufficient_evidence` travels back as itself and
# is excluded from the agreement in Python, where it is counted and reported separately.
AGREEMENT_PAIRS_SQL = """
WITH explicit AS (
    SELECT DISTINCT ON (v.listing_lo, v.listing_hi)
           v.listing_lo, v.listing_hi, v.verdict
      FROM autodedup.verdicts v
     WHERE v.kind = 'pair'
       AND v.listing_lo IS NOT NULL
       AND v.listing_hi IS NOT NULL
     ORDER BY v.listing_lo, v.listing_hi, v.decided_at DESC, v.id DESC
),
confirmed AS (
    SELECT DISTINCT ON (v.cluster_key) v.cluster_key, v.verdict
      FROM autodedup.verdicts v
     WHERE v.kind = 'cluster' AND v.cluster_key IS NOT NULL
     ORDER BY v.cluster_key, v.decided_at DESC, v.id DESC
),
implied AS (
    SELECT DISTINCT ma.listing_id AS listing_lo, mb.listing_id AS listing_hi
      FROM confirmed cf
      JOIN autodedup.clusters c
        ON c.cluster_key = cf.cluster_key
       AND c.generation = %(generation)s::text
       AND c.size <= %(max_cluster_size)s::int
      JOIN autodedup.cluster_members ma ON ma.cluster_key = cf.cluster_key
      JOIN autodedup.cluster_members mb
        ON mb.cluster_key = cf.cluster_key AND mb.listing_id > ma.listing_id
     WHERE cf.verdict = 'same'
),
operator AS (
    SELECT e.listing_lo, e.listing_hi, e.verdict, 'explicit'::text AS source
      FROM explicit e
    UNION ALL
    SELECT i.listing_lo, i.listing_hi, 'same'::text, 'implied'::text
      FROM implied i
     WHERE NOT EXISTS (
         SELECT 1 FROM explicit e2
          WHERE e2.listing_lo = i.listing_lo AND e2.listing_hi = i.listing_hi
     )
),
judged AS (
    SELECT DISTINCT ON (j.listing_lo, j.listing_hi)
           j.listing_lo, j.listing_hi, j.verdict, j.tier, j.model
      FROM autodedup.judgements j
     WHERE j.tier IN ('gold', 'vision', 'text')
     ORDER BY j.listing_lo, j.listing_hi,
              CASE j.tier WHEN 'gold' THEN 0 WHEN 'vision' THEN 1 ELSE 2 END,
              j.created_at DESC
)
SELECT o.listing_lo, o.listing_hi, o.verdict, o.source, g.verdict, g.tier, g.model
FROM operator o
JOIN judged g ON g.listing_lo = o.listing_lo AND g.listing_hi = o.listing_hi
WHERE o.verdict <> 'unsure'
ORDER BY o.listing_lo, o.listing_hi
"""

# The bound, reported rather than hidden: confirmed groups too large to expand. `EXISTS` on any
# `same` row (not the latest one) is deliberate — this is a diagnostic about the cap, and a
# group whose confirmation was later re-decided is still a group the cap would have expanded.
AGREEMENT_OVERSIZE_SQL = """
SELECT count(*)
FROM autodedup.clusters c
WHERE c.generation = %(generation)s::text
  AND c.size > %(max_cluster_size)s::int
  AND EXISTS (
      SELECT 1 FROM autodedup.verdicts v
       WHERE v.kind = 'cluster' AND v.cluster_key = c.cluster_key AND v.verdict = 'same'
  )
"""
