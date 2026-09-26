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
    # Migration 538 / rule E58. WHICH pass the ruling was taken on, WHICH SET of adverts it
    # was about, and whether that set is still this group's — the three facts that turn a
    # carried-over verdict from a silent lie into a visible hint.
    "verdict_generation",
    "verdict_member_ids",
    "verdict_applies",
)

# The FROM is its own constant so the COUNT behind "20 of N" runs the SAME joins as the page
# it counts. The verdict LATERAL is not decoration — `_CLUSTER_WHERE` filters on it — and a
# count that dropped it would answer a different question from the list above it.
#
# A CLUSTER VERDICT IS A STATEMENT ABOUT A SET OF LISTINGS (E58), so it is matched on that set
# and not on the key. `mem.ids` is this group's current membership IN THIS GENERATION; the
# ruling APPLIES when the operator's recorded `member_ids` equal it, and otherwise the group
# reads as unreviewed and the row carries the earlier ruling as a hint. That is the whole
# repair: promoting g5 re-stamped 836 of g4's keys, and 21 of the operator's 224 confirmations
# landed on a group whose membership had moved under them — 11 grown by a bridge, 10 absorbed.
#
# THE RULING THAT APPLIES WINS, and only when none does is the latest carried as a hint. The
# order is `applies` first, `decided_at` second — one spelling, shared with
# `_CLUSTER_VERDICT_LATERAL` behind the progress strip, which selects the applying ruling and
# nothing else. Newest-first alone would diverge from it the moment a key holds a ruling per
# pass (which is exactly what migration 538's generation-scoped verdict key now allows): the
# queue would read a g5 ruling while browsing g4, call the group unreviewed, and the strip
# would count the same group as done off the g4 ruling it still holds.
#
# LEGACY ROWS (no `member_ids`: taken before migration 538) keep the pre-538 behaviour — they
# apply to their own generation, and to any generation when they carry none. There is no
# faithful record of what those operators saw, and inventing one from today's clustering is the
# defect, not the fix.
_CLUSTER_FROM = """
FROM autodedup.clusters c
LEFT JOIN LATERAL (
    SELECT array_agg(m.listing_id ORDER BY m.listing_id) AS ids
      FROM autodedup.cluster_members m
     WHERE m.generation = c.generation AND m.cluster_key = c.cluster_key
) mem ON true
LEFT JOIN LATERAL (
    SELECT vv.verdict, vv.note, vv.reasons, vv.decided_by, vv.decided_at,
           vv.generation, vv.member_ids,
           (vv.member_ids IS NOT NULL
            AND vv.member_ids = coalesce(mem.ids, '{}'::bigint[]))
           OR (vv.member_ids IS NULL
               AND (vv.generation IS NULL OR vv.generation = c.generation)) AS applies
      FROM autodedup.verdicts vv
     WHERE vv.kind = 'cluster' AND vv.cluster_key = c.cluster_key
     ORDER BY applies DESC, vv.decided_at DESC, vv.id DESC
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
    v.verdict, v.note, v.reasons, v.decided_by, v.decided_at,
    v.generation, v.member_ids, coalesce(v.applies, false)
"""
    + _CLUSTER_FROM
)

# THE THREE STORED NEGATIVES, in ONE place (D39). The page offers ONE word for all of them —
# "Ruzne" — while the store keeps the two finer values migration 532 wrote and rewrites no row,
# so a filter asking for `different` alone would drop a ruling taken last week out of its own
# queue. The SQL literal below is built off this tuple rather than typed a second time.
NEGATIVE_VERDICTS: tuple[str, ...] = (
    "different",
    "same_building_different_unit",
    "same_project_different_unit",
)

# Does this row carry the verdict the filter asked for? `different` is the widened one; every
# other value is itself. ONE fragment, so the groups queue and the residual queue cannot come
# to mean two different things by one word.
_VERDICT_MATCHES = (
    "(v.verdict = %(verdict)s::text\n"
    "           OR (%(verdict)s::text = 'different' AND v.verdict IN ("
    + ", ".join(f"'{value}'" for value in NEGATIVE_VERDICTS)
    + ")))"
)

# `verdict = 'unreviewed'` is the ABSENCE of a row, which is why the verdict filter is one arm
# of this predicate and not a join condition: filtering in the LATERAL would hand back every
# cluster with its verdict blanked instead of the clusters that carry that verdict.
_CLUSTER_WHERE = (
    """
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
  -- THE FILTER READS THE RULING THAT APPLIES, never the row that merely exists (E58).
  -- `unreviewed` is "no ruling this group can be said to carry" — an absent verdict OR one
  -- taken on a different set of adverts; `changed` is the new arm that asks for exactly the
  -- second half, which is the queue the operator needs after a promotion; every other value
  -- is a verdict that both matches and applies.
  AND (%(verdict)s::text IS NULL
       OR (%(verdict)s::text = 'unreviewed'
           AND (v.verdict IS NULL OR NOT v.applies))
       OR (%(verdict)s::text = 'changed'
           AND v.verdict IS NOT NULL AND NOT v.applies)
       OR (%(verdict)s::text NOT IN ('unreviewed', 'changed')
           AND """
    + _VERDICT_MATCHES
    + """ AND v.applies))
"""
)

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

# ONE ROW, DETERMINISTICALLY. `(generation, cluster_key)` is the primary key since migration
# 538, so a key alone can match a group per pass and an unqualified caller (the route allows
# `generation` to be absent) would get whichever row the plan handed back first — the dialog's
# members, pairs and stale-verdict hint all derive from it. Newest-changed first, generation as
# the tie-break, one row.
GROUP_ONE_SQL = (
    _CLUSTER_SELECT
    + """
WHERE c.cluster_key = %(cluster_key)s::bigint
  AND (%(generation)s::text IS NULL OR c.generation = %(generation)s::text)
ORDER BY c.last_changed_at DESC, c.generation DESC
LIMIT 1
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
#
# The three LATERALs are spelled ONCE and driven by whichever expression names the listing,
# because the candidate-group queue (§12, E56) reads the identical card over a set of LISTING
# ids rather than over cluster membership. A second copy of them is a second frame order, and
# the day the two diverge the carousel opens on a photo the cover does not name.
def _card_photos(listing: str) -> str:
    return f"""
LEFT JOIN LATERAL (
    SELECT i.storage_path, i.sreality_url
      FROM images i
     WHERE i.listing_id = {listing}
     ORDER BY i.sequence NULLS LAST, i.id
     LIMIT 1
) cover ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS n FROM images i WHERE i.listing_id = {listing}
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
           WHERE i.listing_id = {listing}
           ORDER BY i.sequence NULLS LAST, i.id
           LIMIT %(card_frames)s::int
      ) f
) frames ON true
"""


_MEMBER_CARD_SELECT = """
    l.source, l.source_url, l.category_main, l.category_type, l.disposition,
    l.area_m2, l.floor, l.price_czk, l.first_seen_at, l.last_seen_at, l.is_active,
    cover.storage_path, cover.sreality_url, coalesce(gallery.n, 0),
    coalesce(frames.images, '[]'::json)
"""

GROUP_MEMBERS_SQL = (
    """
SELECT
    m.cluster_key, m.listing_id,
"""
    + _MEMBER_CARD_SELECT
    + """
FROM autodedup.cluster_members m
LEFT JOIN listings l ON l.id = m.listing_id
"""
    + _card_photos("m.listing_id")
    + """
WHERE m.generation = %(generation)s::text
  AND m.cluster_key = any(%(keys)s::bigint[])
ORDER BY m.cluster_key, m.listing_id
"""
)

# The same card, over a SET OF LISTINGS rather than a cluster: the candidate-group queue packs
# adverts from several clusters (and unclustered ones) onto one card, so its members cannot be
# read out of `cluster_members`. ONE statement per page over every listing the page shows —
# never one per group, which is the N+1 the groups queue avoids by keying on `any(keys)`.
LISTING_CARD_COLUMNS: tuple[str, ...] = MEMBER_COLUMNS[1:]

LISTING_CARDS_SQL = (
    """
SELECT
    l.id,
"""
    + _MEMBER_CARD_SELECT
    + """
FROM listings l
"""
    + _card_photos("l.id")
    + """
WHERE l.id = any(%(ids)s::bigint[])
ORDER BY l.id
"""
)

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
WHERE p.generation = %(generation)s::text
  AND p.listing_lo = any(%(ids)s::bigint[])
  AND p.listing_hi = any(%(ids)s::bigint[])
ORDER BY p.score DESC NULLS LAST, p.listing_lo, p.listing_hi
"""
)

PAIR_ONE_SQL = (
    "SELECT"
    + _PAIR_SELECT_LIST
    + """
FROM autodedup.pairs p
WHERE p.generation = %(generation)s::text
  AND p.listing_lo = %(listing_lo)s::bigint AND p.listing_hi = %(listing_hi)s::bigint
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
JOIN autodedup.cluster_members mb
  ON mb.generation = ma.generation AND mb.cluster_key = ma.cluster_key
JOIN autodedup.pairs p
  ON p.generation = ma.generation
 AND p.listing_lo = ma.listing_id AND p.listing_hi = mb.listing_id
LEFT JOIN LATERAL (
    SELECT count(*) AS n
      FROM autodedup.judgements j
     WHERE j.listing_lo = p.listing_lo AND j.listing_hi = p.listing_hi
) judged ON true
WHERE ma.generation = %(generation)s::text
  AND ma.cluster_key = any(%(keys)s::bigint[])
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
    # Migration 538. Null on every pair-grain row by construction (the table's own CHECK):
    # a pair ruling is about two listings and belongs to no generation (E58).
    "generation",
    "member_ids",
)

_VERDICT_SELECT_LIST = """
    v.id, v.kind, v.cluster_key, v.listing_lo, v.listing_hi, v.verdict, v.weight, v.note,
    v.reasons, v.decided_by, v.decided_at, v.generation, v.member_ids
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
    # The pageable gallery, `a_images[0]` being the very frame `a_cover_*` names.
    "a_images",
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
    "b_images",
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
# The per-side photo LATERALs are display, so the count never runs them.
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

# ONE SIDE'S PHOTOS, spelled once and applied to both — the cover, the whole-album count, and
# the first `card_frames` frames. A residual row shows the SAME gallery a group card shows
# (the operator's own request: "click through the images on the residual page, same way I'm on
# the groups page"), so the frame order is `GROUP_MEMBERS_SQL`'s to the letter — a second
# spelling of it would open the carousel on a photo the cover does not name. Capped IN THE
# STATEMENT for the reason the group card is: trimming after the fetch still drags a 120-frame
# album across the wire for both sides of all 20 rows. Three bounded LATERALs per side, none
# of them per-image, and the count statement runs none of them — this fragment is display.
def _residual_photos(side: str, listing: str) -> str:
    return f"""
LEFT JOIN LATERAL (
    SELECT i.storage_path, i.sreality_url FROM images i
     WHERE i.listing_id = p.{listing} ORDER BY i.sequence NULLS LAST, i.id LIMIT 1
) c{side} ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS n FROM images i WHERE i.listing_id = p.{listing}
) g{side} ON true
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
           WHERE i.listing_id = p.{listing}
           ORDER BY i.sequence NULLS LAST, i.id
           LIMIT %(card_frames)s::int
      ) f
) f{side} ON true
"""


_RESIDUAL_PHOTOS = _residual_photos("a", "listing_lo") + _residual_photos("b", "listing_hi")

_RESIDUAL_FILTERS = (
    """
WHERE p.generation = %(generation)s::text
  AND p.score >= %(min_score)s::real
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
       OR """
    + _VERDICT_MATCHES
    + """)
"""
)

# WHAT MAKES A PAIR RESIDUAL, in one place: the two listings are not in ONE cluster of this
# generation. Its own constant because the validation-progress sample below asks the identical
# question — a second spelling of it would let the counter count a cohort the queue does not show.
_RESIDUAL_UNCLUSTERED = """  AND NOT EXISTS (
      SELECT 1
        FROM autodedup.cluster_members ma
        JOIN autodedup.cluster_members mb
          ON mb.generation = ma.generation AND mb.cluster_key = ma.cluster_key
         AND mb.listing_id = p.listing_hi
       WHERE ma.generation = %(generation)s::text
         AND ma.listing_id = p.listing_lo
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
    coalesce(fa.images, '[]'::json),
    lb.source, lb.source_url, lb.category_main, lb.category_type, lb.disposition,
    lb.area_m2, lb.floor, lb.total_floors, lb.price_czk, lb.first_seen_at, lb.last_seen_at,
    lb.is_active, cb.storage_path, cb.sreality_url, coalesce(gb.n, 0),
    coalesce(fb.images, '[]'::json),
    j.verdict, j.confidence, j.tier,
    v.verdict, v.note, v.reasons, v.decided_by, v.decided_at
"""

RESIDUAL_SQL = (
    _RESIDUAL_SELECT
    + _RESIDUAL_FROM
    + _RESIDUAL_BLOCK
    + _RESIDUAL_PHOTOS
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
    + _RESIDUAL_PHOTOS
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

# The residual half of "20 of N". Same FROM minus the display photo LATERALs, same WHERE minus
# the cursor — the cursor is where the page is, not what the filter selects. The block
# LATERAL is NOT display and stays: the WHERE reads it, and a count that dropped it would
# answer a different question from the list above it.
RESIDUAL_COUNT_SQL = "SELECT count(*)" + _RESIDUAL_FROM + _RESIDUAL_BLOCK + _RESIDUAL_WHERE

# ------------------------------------------------- the candidate groups over that cohort (E56)
#
# Three cheap statements feed `autodedup/candidates.py`, which does the packing in Python.
#
# THE COHORT IS THE PAIR QUEUE'S, to the letter: the same display floor and the same
# `_RESIDUAL_UNCLUSTERED` predicate, and the same inner joins to `listings` — a pair whose
# advert row is gone is not on the pair queue either, and a card cannot render it. What is
# NOT here is the filter bar: zone, block and the rest are applied to the BUILT groups, so the
# packing (and its cache) is one structure per generation rather than one per filter combination.
#
# Only the columns the packing reads: ids, score, zone, the family mask for the header chip and
# the block for the filter. No photos, no text, no features — the page's own statements fetch
# those for the twenty groups it actually shows.
CANDIDATE_PAIR_COLUMNS: tuple[str, ...] = (
    "listing_lo",
    "listing_hi",
    "score",
    "zone",
    "families",
    "block_key",
    "block_grain",
)

CANDIDATE_PAIRS_SQL = (
    """
SELECT p.listing_lo, p.listing_hi, p.score, p.zone, p.families, bl.block_key, bl.block_grain
FROM autodedup.pairs p
JOIN listings la ON la.id = p.listing_lo
JOIN listings lb ON lb.id = p.listing_hi
"""
    + _RESIDUAL_BLOCK
    + """
WHERE p.generation = %(generation)s::text
  AND p.score >= %(min_score)s::real
"""
    + _RESIDUAL_UNCLUSTERED
    + """
ORDER BY p.score DESC, p.listing_lo, p.listing_hi
"""
)

# The LOCKS: every advert this generation already merged, with the group it was merged into.
# A unit is a whole cluster, so this selects ALL members — including the ones no residual pair
# names, which is what keeps a candidate card from showing three adverts of a five-advert group.
CANDIDATE_CLUSTER_MEMBER_COLUMNS: tuple[str, ...] = ("cluster_key", "listing_id")

CANDIDATE_CLUSTER_MEMBERS_SQL = """
SELECT m.cluster_key, m.listing_id
FROM autodedup.cluster_members m
WHERE m.generation = %(generation)s::text
ORDER BY m.cluster_key, m.listing_id
"""

# WHAT THE IN-PROCESS CACHE IS KEYED ON. Three numbers that move whenever the packing would
# change: how many pairs are above the floor, when the newest of them was decided, and how many
# advert-to-cluster locks this generation holds. A new score run moves the first two, a
# re-clustering the third — so a cached index cannot outlive the pass it describes. It is
# deliberately NOT a hash of the rows: the point is to avoid reading them.
CANDIDATE_FINGERPRINT_COLUMNS: tuple[str, ...] = ("n_pairs", "decided_at", "n_locks")

# DELIBERATELY UNFILTERED by the display floor, unlike the cohort it fingerprints — and, since
# migration 538, SCOPED TO THE GENERATION, because that is what the cached index describes. The
# score index is PARTIAL (`where zone = 'band'`), so a `score >= …` predicate cannot use it and
# would make both halves a filtered scan on EVERY page; `(generation, decided_at desc)` serves
# the newest-decided probe as a one-row index scan, and counting the generation's whole pair set
# is a strict superset of counting the cohort — a superset invalidates more often, never less,
# which is the safe direction for a cache. Before 538 this counted EVERY generation's pairs,
# so a score run at g5 silently invalidated g4's cached packing.
CANDIDATE_FINGERPRINT_SQL = """
SELECT
    (SELECT count(*) FROM autodedup.pairs p WHERE p.generation = %(generation)s::text),
    (SELECT max(p.decided_at) FROM autodedup.pairs p
      WHERE p.generation = %(generation)s::text),
    (SELECT count(*) FROM autodedup.cluster_members m
      WHERE m.generation = %(generation)s::text)
"""

# Every pair the operator has ruled on, latest ruling per pair — what makes a candidate group
# "reviewed" (every underlying residual pair carries a verdict) and what the progress strip
# counts. The whole table, because `autodedup.verdicts` is operator-written and small, and
# because the filter has to know the state of groups the current page does not show.
CANDIDATE_VERDICT_COLUMNS: tuple[str, ...] = ("listing_lo", "listing_hi", "verdict")

OPERATOR_PAIR_VERDICTS_SQL = """
SELECT DISTINCT ON (v.listing_lo, v.listing_hi)
       v.listing_lo, v.listing_hi, v.verdict
FROM autodedup.verdicts v
WHERE v.kind = 'pair'
  AND v.listing_lo IS NOT NULL
  AND v.listing_hi IS NOT NULL
ORDER BY v.listing_lo, v.listing_hi, v.decided_at DESC, v.id DESC
"""

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

# SCOPED TO ONE PASS (migration 538). `autodedup.pairs` holds every generation ever scored, so
# an unscoped histogram counts four engines at once — the very mix that made the validation
# panel read band 75/67 where g4 reads 73/18 (M42).
PAIR_ZONES_SQL = """
SELECT coalesce(p.zone, 'unscored') AS zone, count(*) AS n
FROM autodedup.pairs p
WHERE p.generation = %(generation)s::text
GROUP BY 1
ORDER BY 1
"""

CERTIFICATE_COUNT_COLUMNS: tuple[str, ...] = ("certificate", "n")

CERTIFICATE_COUNTS_SQL = """
SELECT split_part(p.decision, ':', 2) AS certificate, count(*) AS n
FROM autodedup.pairs p
WHERE p.generation = %(generation)s::text
  AND split_part(p.decision, ':', 1) = 'certificate'
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

# One row per pass: the picker's vocabulary, the batch passes (the evaluation lab) newest first
# and a real-time generation last. An UNNAMED view reads the live stream once it is live
# (`LIVE_STREAM_SQL`), else this order's first pass (`LATEST_GENERATION_SQL`) — see
# api/routes/autodedup.py `_resolve_generation`.
GENERATION_COUNTS_SQL = """
SELECT
    c.generation,
    count(*)                                          AS n_clusters,
    coalesce(sum(c.size), 0)                          AS n_members,
    count(*) FILTER (WHERE c.status = 'conflict')     AS n_conflicted,
    max(c.last_changed_at)                            AS last_changed_at
FROM autodedup.clusters c
GROUP BY c.generation
ORDER BY (left(c.generation, 2) = 'rt'), max(c.last_changed_at) DESC
"""

# WHICH pass a validation view reads when the caller names none, step one (E914): the live
# stream `rt` — the generation the worker's lane reconciles production from — but only once it
# IS live: a seed of this design built it (`rt_seed_version:rt` = `incremental.SEED_VERSION`)
# and its build phase ended (`rt_bootstrap:rt` false). The 09-21 `rt` was seeded before F2 and
# holds groups this build would not draw; opening every review page on it mid-trial would show
# hundreds of properties as split proposals the live engine never made.
LIVE_STREAM_SQL = """
SELECT s.key, s.value
FROM autodedup.settings s
WHERE s.key = ANY(%(keys)s::text[])
"""

# Step two, when the stream is not live: the newest BATCH pass. A real-time generation is
# rewritten every pass, so by recency alone it would always be "newest" — which is how the
# operator's pair links 404'd on 2026-09-20 (the pairs lived in g6; the default had silently
# become `rt`). It is ordered last, and read only when named or when it is live.
LATEST_GENERATION_SQL = """
SELECT c.generation
FROM autodedup.clusters c
ORDER BY (left(c.generation, 2) = 'rt'), c.last_changed_at DESC
LIMIT 1
"""

VERDICT_COUNT_COLUMNS: tuple[str, ...] = ("kind", "verdict", "n")

# RULINGS, NOT ROWS (migration 574): the store keeps every correction, so a count over rows would
# count a pair ruled `same` and later withdrawn once as each. The newest row per pair and per
# (group key, pass) is the ruling, exactly as every engine reader takes it.
_NEWEST_RULINGS = """
FROM (
    SELECT DISTINCT ON (x.kind, x.listing_lo, x.listing_hi, x.cluster_key,
                        coalesce(x.generation, ''::text))
           x.kind, x.verdict, x.reasons
      FROM autodedup.verdicts x
     ORDER BY x.kind, x.listing_lo, x.listing_hi, x.cluster_key,
              coalesce(x.generation, ''::text), x.decided_at DESC, x.id DESC
) v
"""

VERDICT_COUNTS_SQL = (
    """
SELECT v.kind, v.verdict, count(*) AS n
"""
    + _NEWEST_RULINGS
    + """
GROUP BY 1, 2
ORDER BY 1, 2
"""
)

# The reason histogram (migration 533): WHAT the operator saw, counted per grain. Pair and
# cluster stay separate columns on the page because they answer different questions — a chip
# on a pair names the discriminator one edge missed, the same chip on a cluster names why a
# whole proposal was wrong — and summing them would hide both. `unnest` is a LATERAL over the
# array, so a verdict with no reason contributes no row at all rather than a null bucket.
REASON_COUNT_COLUMNS: tuple[str, ...] = ("kind", "reason", "n")

REASON_COUNTS_SQL = (
    """
SELECT v.kind, r.reason, count(*) AS n
"""
    + _NEWEST_RULINGS
    + """
CROSS JOIN LATERAL unnest(v.reasons) AS r(reason)
GROUP BY 1, 2
ORDER BY 1, 2
"""
)

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

# THE STORE IS A LEDGER (Decision 8, migration 574, E919). A ruling is never overwritten: a flip,
# a withdrawal (`unsure`) or a re-ruling with another note is a NEW row, and per pair the newest
# row (`decided_at desc, id desc`) is the ruling every reader obeys -- the lane's must-links
# (`incremental_sql.RT_MUST_LINK_SQL`), apply's negatives (`apply_sql.PAIR_VERDICTS_SQL`), the
# labels and every review queue already read it that way.
#
# APPEND ON CHANGE. A write that says exactly what the newest row already says (same verdict,
# note and reasons) appends nothing and answers that newest row, so a double click or a re-merge
# of the same set does not stack identical rows.
#
# BOTH SIDES OF MIGRATION 574. Before it, a partial unique index allowed one row per (pair,
# decider): `ON CONFLICT DO NOTHING` (no target, so it is valid with or without that index) lets
# the conflicting insert fall through, and `restated` then updates that decider's own row in place
# -- the pre-574 behaviour, reached only while the index still exists. After 574 the insert never
# conflicts and `restated` never fires. The code therefore ships BEFORE the migration is applied
# (the migration's header says why the reverse order is an outage).
_VERDICT_RETURNING = """id, kind, cluster_key, listing_lo, listing_hi, verdict, weight, note, reasons,
              decided_by, decided_at, generation, member_ids"""

VERDICT_PAIR_APPEND_SQL = (
    """
WITH newest AS (
    SELECT x.id, x.kind, x.cluster_key, x.listing_lo, x.listing_hi, x.verdict, x.weight, x.note,
           x.reasons, x.decided_by, x.decided_at, x.generation, x.member_ids
      FROM autodedup.verdicts x
     WHERE x.kind = 'pair'
       AND x.listing_lo = %(listing_lo)s::bigint
       AND x.listing_hi = %(listing_hi)s::bigint
     ORDER BY x.decided_at DESC, x.id DESC
     LIMIT 1
), said AS (
    SELECT NOT EXISTS (
        SELECT 1 FROM newest n
         WHERE n.verdict = %(verdict)s::text
           AND n.note IS NOT DISTINCT FROM %(note)s::text
           AND n.reasons = %(reasons)s::text[]
    ) AS changed
), appended AS (
    INSERT INTO autodedup.verdicts (kind, listing_lo, listing_hi, verdict, note, reasons,
                                    decided_by)
    SELECT 'pair', %(listing_lo)s::bigint, %(listing_hi)s::bigint, %(verdict)s::text,
           %(note)s::text, %(reasons)s::text[], %(decided_by)s::text
      FROM said
     WHERE said.changed
    ON CONFLICT DO NOTHING
    RETURNING """
    + _VERDICT_RETURNING
    + """
), restated AS (
    UPDATE autodedup.verdicts u
       SET verdict = %(verdict)s::text, note = %(note)s::text, reasons = %(reasons)s::text[],
           decided_at = now()
      FROM said
     WHERE said.changed
       AND NOT EXISTS (SELECT 1 FROM appended)
       AND u.kind = 'pair'
       AND u.listing_lo = %(listing_lo)s::bigint
       AND u.listing_hi = %(listing_hi)s::bigint
       AND u.decided_by = %(decided_by)s::text
    RETURNING u.id, u.kind, u.cluster_key, u.listing_lo, u.listing_hi, u.verdict, u.weight,
              u.note, u.reasons, u.decided_by, u.decided_at, u.generation, u.member_ids
)
SELECT * FROM appended
UNION ALL
SELECT * FROM restated
UNION ALL
SELECT n.* FROM newest n CROSS JOIN said WHERE NOT said.changed
"""
)

# STAMPED WITH THE SET IT IS ABOUT (E58, migration 538). `generation` and `member_ids` are not
# optional extras on this write: the ruling is a statement about those adverts. A fresh ruling
# has its `member_ids` resolved by the route from the store for that (generation, cluster_key),
# never from the request body; a correction on the rulings page (`supersedes`) copies both from
# the ruling it replaces, because it is a new word about THAT set.
#
# THE KEY CARRIES THE PASS: the newest row per (cluster_key, coalesce(generation, '')) is the
# group's ruling, so ruling the g5 group 38324 never touches what the operator said about g4's.
# A changed member set under the same key is a change, and so appends.
VERDICT_CLUSTER_APPEND_SQL = (
    """
WITH newest AS (
    SELECT x.id, x.kind, x.cluster_key, x.listing_lo, x.listing_hi, x.verdict, x.weight, x.note,
           x.reasons, x.decided_by, x.decided_at, x.generation, x.member_ids
      FROM autodedup.verdicts x
     WHERE x.kind = 'cluster'
       AND x.cluster_key = %(cluster_key)s::bigint
       AND coalesce(x.generation, ''::text) = coalesce(%(generation)s::text, ''::text)
     ORDER BY x.decided_at DESC, x.id DESC
     LIMIT 1
), said AS (
    SELECT NOT EXISTS (
        SELECT 1 FROM newest n
         WHERE n.verdict = %(verdict)s::text
           AND n.note IS NOT DISTINCT FROM %(note)s::text
           AND n.reasons = %(reasons)s::text[]
           AND n.member_ids IS NOT DISTINCT FROM %(member_ids)s::bigint[]
    ) AS changed
), appended AS (
    INSERT INTO autodedup.verdicts (kind, cluster_key, verdict, note, reasons, decided_by,
                                    generation, member_ids)
    SELECT 'cluster', %(cluster_key)s::bigint, %(verdict)s::text, %(note)s::text,
           %(reasons)s::text[], %(decided_by)s::text, %(generation)s::text,
           %(member_ids)s::bigint[]
      FROM said
     WHERE said.changed
    ON CONFLICT DO NOTHING
    RETURNING """
    + _VERDICT_RETURNING
    + """
), restated AS (
    UPDATE autodedup.verdicts u
       SET verdict = %(verdict)s::text, note = %(note)s::text, reasons = %(reasons)s::text[],
           member_ids = %(member_ids)s::bigint[], decided_at = now()
      FROM said
     WHERE said.changed
       AND NOT EXISTS (SELECT 1 FROM appended)
       AND u.kind = 'cluster'
       AND u.cluster_key = %(cluster_key)s::bigint
       AND coalesce(u.generation, ''::text) = coalesce(%(generation)s::text, ''::text)
       AND u.decided_by = %(decided_by)s::text
    RETURNING u.id, u.kind, u.cluster_key, u.listing_lo, u.listing_hi, u.verdict, u.weight,
              u.note, u.reasons, u.decided_by, u.decided_at, u.generation, u.member_ids
)
SELECT * FROM appended
UNION ALL
SELECT * FROM restated
UNION ALL
SELECT n.* FROM newest n CROSS JOIN said WHERE NOT said.changed
"""
)

# The ruling a correction names (`POST /autodedup/verdict` `supersedes`), and the newest ruling
# of its key: a correction is accepted only while what the page showed is still the newest word,
# so a stale tab answers 409 instead of silently ruling over a ruling it never saw.
VERDICT_ONE_SQL = (
    "SELECT"
    + _VERDICT_SELECT_LIST
    + """
FROM autodedup.verdicts v
WHERE v.id = %(id)s::bigint
"""
)

PAIR_NEWEST_RULING_SQL = (
    "SELECT"
    + _VERDICT_SELECT_LIST
    + """
FROM autodedup.verdicts v
WHERE v.kind = 'pair'
  AND v.listing_lo = %(listing_lo)s::bigint
  AND v.listing_hi = %(listing_hi)s::bigint
ORDER BY v.decided_at DESC, v.id DESC
LIMIT 1
"""
)

CLUSTER_NEWEST_RULING_SQL = (
    "SELECT"
    + _VERDICT_SELECT_LIST
    + """
FROM autodedup.verdicts v
WHERE v.kind = 'cluster'
  AND v.cluster_key = %(cluster_key)s::bigint
  AND coalesce(v.generation, ''::text) = coalesce(%(generation)s::text, ''::text)
ORDER BY v.decided_at DESC, v.id DESC
LIMIT 1
"""
)

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
# Just the ids of ONE generation's group — the membership a whole-cluster ruling fans out over
# AND the set that ruling is stamped with (E58). The server resolves it; a client never names
# it, because a client's idea of the membership is as old as its last fetch.
CLUSTER_MEMBER_IDS_SQL = """
SELECT m.listing_id
FROM autodedup.cluster_members m
WHERE m.generation = %(generation)s::text
  AND m.cluster_key = %(cluster_key)s::bigint
ORDER BY m.listing_id
"""

CLUSTER_EXISTS_SQL = """
SELECT 1 FROM autodedup.clusters
WHERE generation = %(generation)s::text AND cluster_key = %(cluster_key)s::bigint
"""

# Does this clustering pass exist at all? The candidate WRITE names its generation (the card
# says which pass it was packed from), and a name nothing clustered would otherwise build a
# cohort out of every scored pair — every pair is "unclustered" in a pass that does not exist.
GENERATION_EXISTS_SQL = """
SELECT 1 FROM autodedup.clusters WHERE generation = %(generation)s::text LIMIT 1
"""

# ANY generation: a pair verdict is about two adverts, not about the pass that proposed them
# (E58), so a pair this engine scored under g4 is still a pair the operator may rule today. And
# ANY earlier word about the pair: a Browse merge's or a detach's ruling, a group ruling whose set
# holds both adverts (an implied pair), or a must-not-link, is about a pair the engine may never
# have stored (Decision 7 keeps no machine reject), and the operator must be able to correct it
# (the rulings page, E919).
PAIR_EXISTS_SQL = """
SELECT 1
WHERE EXISTS (SELECT 1 FROM autodedup.verdicts x
               WHERE x.kind = 'pair'
                 AND x.listing_lo = %(listing_lo)s::bigint
                 AND x.listing_hi = %(listing_hi)s::bigint)
   OR EXISTS (SELECT 1 FROM autodedup.verdicts c
               WHERE c.kind = 'cluster'
                 AND c.member_ids @> ARRAY[%(listing_lo)s::bigint, %(listing_hi)s::bigint])
   OR EXISTS (SELECT 1 FROM autodedup.must_not_link n
               WHERE n.listing_lo = %(listing_lo)s::bigint
                 AND n.listing_hi = %(listing_hi)s::bigint)
   OR EXISTS (SELECT 1 FROM autodedup.pairs p
               WHERE p.listing_lo = %(listing_lo)s::bigint
                 AND p.listing_hi = %(listing_hi)s::bigint)
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
# ONLY A RULING THAT APPLIES COUNTS AS PROGRESS (E58). A verdict whose recorded `member_ids`
# are not this group's current set is a ruling about a different set of adverts, and counting
# it would tell the operator they are done with a queue they still have to walk. The sample's
# own membership is read here rather than passed in: the strip and the queue must answer the
# same question, and the queue asks the store.
_CLUSTER_VERDICT_LATERAL = """
LEFT JOIN LATERAL (
    SELECT array_agg(m.listing_id ORDER BY m.listing_id) AS ids
      FROM autodedup.cluster_members m
     WHERE m.generation = %(generation)s::text AND m.cluster_key = s.cluster_key
) mem ON true
LEFT JOIN LATERAL (
    SELECT vv.verdict
      FROM autodedup.verdicts vv
     WHERE vv.kind = 'cluster' AND vv.cluster_key = s.cluster_key
       AND ((vv.member_ids IS NOT NULL
             AND vv.member_ids = coalesce(mem.ids, '{}'::bigint[]))
            OR (vv.member_ids IS NULL
                AND (vv.generation IS NULL
                     OR vv.generation = %(generation)s::text)))
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
     WHERE p.generation = %(generation)s::text
       AND p.score >= %(min_score)s::real
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
     WHERE p.generation = %(generation)s::text
       AND p.score >= %(min_score)s::real
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
# THE IMPLIED PAIRS COME OUT OF THE VERDICT, NOT OUT OF TODAY'S CLUSTERING (E58, migration
# 538). `member_ids` is the set the operator was actually looking at, so the label set no
# longer moves when a later pass re-clusters — which is exactly what happened when g5 re-stamped
# 836 of g4's keys and 21 confirmations silently changed what they asserted. The fallback for a
# LEGACY row that carries no set is the members of ITS OWN generation, which is the pre-538
# read and the most that can honestly be said about it — and it answers ONLY when the key names
# one set. A legacy row carries no generation either, and `(generation, cluster_key)` lets the
# same key live in several passes, so an unguarded `array_agg` would hand back the UNION of
# every pass that ever held it: six ids where four were ruled on, and up to 45 "the operator
# said same" pairs invented for the D6 gate out of the 6 they actually asserted. The
# `count(DISTINCT m.generation) = 1` guard returns NULL instead, and a row with no set implies
# nothing.
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
    SELECT DISTINCT ON (v.cluster_key)
           v.cluster_key, v.verdict, coalesce(v.member_ids, fb.ids) AS member_ids
      FROM autodedup.verdicts v
      LEFT JOIN LATERAL (
          SELECT CASE WHEN count(DISTINCT m.generation) = 1
                      THEN array_agg(m.listing_id ORDER BY m.listing_id) END AS ids
            FROM autodedup.cluster_members m
           WHERE m.cluster_key = v.cluster_key
             AND (v.generation IS NULL OR m.generation = v.generation)
      ) fb ON v.member_ids IS NULL
     WHERE v.kind = 'cluster' AND v.cluster_key IS NOT NULL
     ORDER BY v.cluster_key, v.decided_at DESC, v.id DESC
),
implied AS (
    SELECT DISTINCT ma.id AS listing_lo, mb.id AS listing_hi
      FROM confirmed cf
      CROSS JOIN LATERAL unnest(cf.member_ids) AS ma(id)
      CROSS JOIN LATERAL unnest(cf.member_ids) AS mb(id)
     WHERE cf.verdict = 'same'
       AND cf.member_ids IS NOT NULL
       AND coalesce(array_length(cf.member_ids, 1), 0) <= %(max_cluster_size)s::int
       AND mb.id > ma.id
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

# The bound, reported rather than hidden: confirmed groups too large to expand. Counted over
# the VERDICTS and their own member sets, for the same reason the expansion is (E58) — a
# diagnostic about the cap must be measured on the sets the cap was applied to. Any `same` row
# counts, not only the latest: a confirmation later re-decided is still one the cap would have
# expanded.
AGREEMENT_OVERSIZE_SQL = """
SELECT count(*)
FROM autodedup.verdicts v
LEFT JOIN LATERAL (
    SELECT CASE WHEN count(DISTINCT m.generation) = 1
                THEN array_agg(m.listing_id ORDER BY m.listing_id) END AS ids
      FROM autodedup.cluster_members m
     WHERE m.cluster_key = v.cluster_key
       AND (v.generation IS NULL OR m.generation = v.generation)
) fb ON v.member_ids IS NULL
WHERE v.kind = 'cluster'
  AND v.cluster_key IS NOT NULL
  AND v.verdict = 'same'
  AND coalesce(array_length(coalesce(v.member_ids, fb.ids), 1), 0)
      > %(max_cluster_size)s::int
"""

# ------------------------------------------------------------ the proposed splits (Decision 9)

# Every advert of every LIVE property the generation touches (`autodedup/proposed_splits.py`
# decides which are proposals), with the group it holds (NULL = none) and whether the generation
# SAW it (a group member, a scored pair's side, or — for the live stream — a fingerprint the lane
# holds); with `property_id`, that property only.
PROPOSED_SPLIT_ADVERTS_SQL = """
WITH grouped AS (
    SELECT s.listing_id, max(s.cluster_key) AS cluster_key
    FROM (
        SELECT m.listing_id, m.cluster_key FROM autodedup.cluster_members m
         WHERE m.generation = %(generation)s::text
        UNION ALL
        SELECT e.listing_id, NULL::bigint FROM autodedup.pairs p
         CROSS JOIN LATERAL (VALUES (p.listing_lo), (p.listing_hi)) AS e(listing_id)
         WHERE p.generation = %(generation)s::text
        UNION ALL
        SELECT f.listing_id, NULL::bigint FROM autodedup.rt_fp f
         WHERE f.generation = %(generation)s::text
    ) s
    GROUP BY s.listing_id
), touched AS (
    SELECT DISTINCT l.property_id FROM grouped g JOIN public.listings l ON l.id = g.listing_id
     WHERE %(property_id)s::bigint IS NULL OR l.property_id = %(property_id)s::bigint
)
SELECT l.property_id, l.id, l.source, l.is_active, pr.repr_listing_ref_id,
       g.cluster_key, g.listing_id IS NOT NULL
  FROM touched t
  JOIN public.properties pr ON pr.id = t.property_id AND pr.status = 'active'
  JOIN public.listings l ON l.property_id = pr.id
  LEFT JOIN grouped g ON g.listing_id = l.id
 ORDER BY l.property_id, l.id
"""

# ------------------------------------------------------------------- the rulings page (E919)
#
# EVERY operator ruling, in one list, beside the engine's current view of it
# (`GET /autodedup/rulings`, the page `/autodedup/rulings`). No other surface lists the rulings:
# a queue shows a ruling only while its pair or group sits in the chosen generation's queue.
#
# THE NEWEST ROW IS THE RULING (migration 574): per pair, and per (group key, pass). `status` is
# derived from the history, never parsed from a note: `standing` (the newest row states
# something), `withdrawn` (the newest is `unsure` and an earlier row stated something) or `unsure`
# (nothing else was ever said).
#
# FOUR SOURCES AT PAIR GRAIN, each named as what it is:
#   * `pair`          a ruling typed against the pair (a queue, a split, the pair page, a detach);
#   * `browse_merge`  a Browse merge's `same`, linked to its `operator_merges` group (564);
#   * `implied`       a member pair of a group whose newest ruling is `same` and which carries no
#                     pair-grain row of its own (explicit beats implied, as in the labels lane);
#                     the lane never binds it (E910 reads pair grain only), the page says so;
#   * `must_not_link` an operator veto with no ruling row behind it.
# Each pair appears ONCE across the four, so `(decided_at, listing_lo, listing_hi)` is the
# keyset.
#
# THE ENGINE'S VIEW is read in ONE generation (the route resolves it as every view does: `rt`
# once live). `engine_view`: `together` (both in one group), `apart` (both seen, not in one
# group) or `unseen` (the pass never read one of them -- outside the trial scope, say).
# `agreement` puts the ruling against production and the engine: a standing `same` disagrees when
# the two are apart now or the engine holds them apart; a standing negative when they are
# together now or in one engine group; anything else (a withdrawal, `unsure`) is `none`.


def _engine_seen(listing: str) -> str:
    """Did the generation READ this advert: a group member, a fingerprint (the live stream) or
    a stored pair's side -- `PROPOSED_SPLIT_ADVERTS_SQL`'s definition, per advert."""
    return f"""(EXISTS (SELECT 1 FROM autodedup.cluster_members m
                  WHERE m.generation = %(generation)s::text AND m.listing_id = {listing})
         OR EXISTS (SELECT 1 FROM autodedup.rt_fp f
                     WHERE f.generation = %(generation)s::text AND f.listing_id = {listing})
         OR EXISTS (SELECT 1 FROM autodedup.pairs q
                     WHERE q.generation = %(generation)s::text AND q.listing_lo = {listing})
         OR EXISTS (SELECT 1 FROM autodedup.pairs q
                     WHERE q.generation = %(generation)s::text AND q.listing_hi = {listing}))"""


_NEGATIVE_LIST = ", ".join(f"'{value}'" for value in NEGATIVE_VERDICTS)

# The ruling against the state of production and the engine, ONE spelling for both grains.
_AGREEMENT = f"""
           CASE WHEN w.status <> 'standing' OR w.verdict = 'unsure' THEN 'none'
                WHEN w.verdict = 'same'
                     THEN CASE WHEN NOT w.together_now OR w.engine_view = 'apart'
                               THEN 'disagrees' ELSE 'agrees' END
                WHEN w.verdict IN ({_NEGATIVE_LIST})
                     THEN CASE WHEN w.together_now OR w.engine_view = 'together'
                               THEN 'disagrees' ELSE 'agrees' END
                ELSE 'none' END AS agreement,
           CASE WHEN w.verdict IN ({_NEGATIVE_LIST}) THEN 'different'
                ELSE w.verdict END AS verdict_word
"""

RULING_PAIR_COLUMNS: tuple[str, ...] = (
    "ruling_id",
    "ruling_kind",
    "listing_lo",
    "listing_hi",
    "verdict",
    "status",
    "source",
    "merge_group_id",
    "group_cluster_key",
    "group_generation",
    "note",
    "reasons",
    "decided_by",
    "decided_at",
    "n_rows",
    "must_not_link",
    "property_lo",
    "property_hi",
    "together_now",
    "adverts_on_property",
    "obec_kod",
    "obec_name",
    "cast_obce_kod",
    "cast_obce_name",
    "street_lo",
    "cp_lo",
    "street_hi",
    "cp_hi",
    "zone",
    "score",
    "decision",
    "guard_veto",
    "engine_decided_at",
    "engine_group_lo",
    "engine_group_hi",
    "seen_lo",
    "seen_hi",
    "engine_view",
    "agreement",
)

_RULINGS_PAIR_FROM = (
    """
WITH pair_rows AS (
    SELECT x.id, x.listing_lo, x.listing_hi, x.verdict, x.note, x.reasons, x.decided_by,
           x.decided_at, x.operator_merge_group_id
      FROM autodedup.verdicts x
     WHERE x.kind = 'pair' AND x.listing_lo IS NOT NULL AND x.listing_hi IS NOT NULL
), newest AS (
    SELECT DISTINCT ON (p.listing_lo, p.listing_hi)
           p.id, p.listing_lo, p.listing_hi, p.verdict, p.note, p.reasons, p.decided_by,
           p.decided_at, p.operator_merge_group_id
      FROM pair_rows p
     ORDER BY p.listing_lo, p.listing_hi, p.decided_at DESC, p.id DESC
), history AS (
    SELECT p.listing_lo, p.listing_hi, count(*) AS n_rows,
           bool_or(p.verdict <> 'unsure') AS ever_stated
      FROM pair_rows p
     GROUP BY p.listing_lo, p.listing_hi
), group_rulings AS (
    SELECT DISTINCT ON (x.cluster_key, coalesce(x.generation, ''::text))
           x.id, x.cluster_key, x.generation, x.member_ids, x.verdict, x.note, x.reasons,
           x.decided_by, x.decided_at
      FROM autodedup.verdicts x
     WHERE x.kind = 'cluster'
     ORDER BY x.cluster_key, coalesce(x.generation, ''::text), x.decided_at DESC, x.id DESC
), implied AS (
    SELECT DISTINCT ON (a.lo, b.hi)
           g.id, a.lo AS listing_lo, b.hi AS listing_hi, g.note, g.reasons, g.decided_by,
           g.decided_at, g.cluster_key, g.generation
      FROM group_rulings g
     CROSS JOIN LATERAL unnest(g.member_ids) AS a(lo)
     CROSS JOIN LATERAL unnest(g.member_ids) AS b(hi)
     WHERE g.verdict = 'same'
       AND a.lo < b.hi
       AND NOT EXISTS (SELECT 1 FROM history h
                        WHERE h.listing_lo = a.lo AND h.listing_hi = b.hi)
     ORDER BY a.lo, b.hi, g.decided_at DESC, g.id DESC
), rulings AS (
    SELECT n.id AS ruling_id, 'pair'::text AS ruling_kind, n.listing_lo, n.listing_hi,
           n.verdict,
           CASE WHEN n.verdict <> 'unsure' THEN 'standing'
                WHEN h.ever_stated THEN 'withdrawn'
                ELSE 'unsure' END AS status,
           CASE WHEN n.operator_merge_group_id IS NOT NULL THEN 'browse_merge'
                ELSE 'pair' END AS source,
           n.operator_merge_group_id AS merge_group_id,
           NULL::bigint AS group_cluster_key, NULL::text AS group_generation,
           n.note, n.reasons, n.decided_by, n.decided_at, h.n_rows
      FROM newest n
      JOIN history h ON h.listing_lo = n.listing_lo AND h.listing_hi = n.listing_hi
    UNION ALL
    SELECT i.id, 'cluster'::text, i.listing_lo, i.listing_hi, 'same'::text, 'standing'::text,
           'implied'::text, NULL::uuid, i.cluster_key, i.generation, i.note, i.reasons,
           i.decided_by, i.decided_at, 0::bigint
      FROM implied i
    UNION ALL
    SELECT NULL::bigint, 'must_not_link'::text, m.listing_lo, m.listing_hi, 'different'::text,
           'standing'::text, 'must_not_link'::text, NULL::uuid, NULL::bigint, NULL::text,
           m.reason, '{}'::text[], m.source, m.created_at, 0::bigint
      FROM autodedup.must_not_link m
     WHERE NOT EXISTS (SELECT 1 FROM history h
                        WHERE h.listing_lo = m.listing_lo AND h.listing_hi = m.listing_hi)
       AND NOT EXISTS (SELECT 1 FROM implied i
                        WHERE i.listing_lo = m.listing_lo AND i.listing_hi = m.listing_hi)
), joined AS (
    SELECT r.ruling_id, r.ruling_kind, r.listing_lo, r.listing_hi, r.verdict, r.status,
           r.source, r.merge_group_id, r.group_cluster_key, r.group_generation, r.note,
           r.reasons, r.decided_by, r.decided_at, r.n_rows,
           mnl.source AS must_not_link,
           la.property_id AS property_lo, lb.property_id AS property_hi,
           coalesce(la.property_id = lb.property_id, false) AS together_now,
           ll.obec_kod, ll.obec_name, ll.cast_obce_kod, ll.cast_obce_name,
           ll.street_name AS street_lo, ll.house_number_cp AS cp_lo,
           lh.street_name AS street_hi, lh.house_number_cp AS cp_hi,
           p.zone, p.score, p.decision, p.guard_veto, p.decided_at AS engine_decided_at,
           ca.cluster_key AS engine_group_lo, cb.cluster_key AS engine_group_hi,
           """
    + _engine_seen("r.listing_lo")
    + """ AS seen_lo,
           """
    + _engine_seen("r.listing_hi")
    + """ AS seen_hi
      FROM rulings r
      LEFT JOIN public.listings la ON la.id = r.listing_lo
      LEFT JOIN public.listings lb ON lb.id = r.listing_hi
      LEFT JOIN public.listing_location ll ON ll.listing_id = r.listing_lo
      LEFT JOIN public.listing_location lh ON lh.listing_id = r.listing_hi
      LEFT JOIN autodedup.must_not_link mnl
             ON mnl.listing_lo = r.listing_lo AND mnl.listing_hi = r.listing_hi
      LEFT JOIN autodedup.pairs p
             ON p.generation = %(generation)s::text
            AND p.listing_lo = r.listing_lo AND p.listing_hi = r.listing_hi
      LEFT JOIN LATERAL (
          SELECT m.cluster_key FROM autodedup.cluster_members m
           WHERE m.generation = %(generation)s::text AND m.listing_id = r.listing_lo
           LIMIT 1
      ) ca ON true
      LEFT JOIN LATERAL (
          SELECT m.cluster_key FROM autodedup.cluster_members m
           WHERE m.generation = %(generation)s::text AND m.listing_id = r.listing_hi
           LIMIT 1
      ) cb ON true
), w AS (
    SELECT j.*,
           CASE WHEN j.engine_group_lo IS NOT NULL AND j.engine_group_lo = j.engine_group_hi
                     THEN 'together'
                WHEN j.seen_lo AND j.seen_hi THEN 'apart'
                ELSE 'unseen' END AS engine_view
      FROM joined j
), v AS (
    SELECT w.*,
"""
    + _AGREEMENT
    + """
      FROM w
)
"""
)

# The filter arms, shared by the page and its counts so "20 of N" counts what the page lists.
# A nullable parameter per filter, never interpolated text (the module's rule).
_RULINGS_FILTERS = (
    """
WHERE (%(verdict)s::text IS NULL OR """
    + _VERDICT_MATCHES
    + """)
  AND (%(source)s::text IS NULL OR v.source = %(source)s::text)
  AND (%(status)s::text IS NULL OR v.status = %(status)s::text)
  AND (%(engine)s::text IS NULL OR v.agreement = %(engine)s::text)
  AND (%(together)s::boolean IS NULL OR v.together_now = %(together)s::boolean)
  AND (%(decided_from)s::timestamptz IS NULL OR v.decided_at >= %(decided_from)s::timestamptz)
  AND (%(decided_to)s::timestamptz IS NULL OR v.decided_at < %(decided_to)s::timestamptz)
  AND (%(obec)s::bigint IS NULL OR v.obec_kod = %(obec)s::bigint)
  AND (%(cast_obce)s::bigint IS NULL OR v.cast_obce_kod = %(cast_obce)s::bigint)
  AND (%(merge_group)s::uuid IS NULL OR v.merge_group_id = %(merge_group)s::uuid)
"""
)

_RULINGS_PAIR_WHERE = (
    _RULINGS_FILTERS
    + """  AND (%(listing)s::bigint IS NULL
       OR v.listing_lo = %(listing)s::bigint OR v.listing_hi = %(listing)s::bigint)
  AND (%(property)s::bigint IS NULL
       OR v.property_lo = %(property)s::bigint OR v.property_hi = %(property)s::bigint)
"""
)

RULINGS_PAIR_SQL = (
    _RULINGS_PAIR_FROM
    + """
SELECT v.ruling_id, v.ruling_kind, v.listing_lo, v.listing_hi, v.verdict, v.status, v.source,
       v.merge_group_id, v.group_cluster_key, v.group_generation, v.note, v.reasons,
       v.decided_by, v.decided_at, v.n_rows, v.must_not_link, v.property_lo, v.property_hi,
       v.together_now,
       CASE WHEN v.together_now
            THEN (SELECT count(*) FROM public.listings l WHERE l.property_id = v.property_lo)
       END AS adverts_on_property,
       v.obec_kod, v.obec_name, v.cast_obce_kod, v.cast_obce_name, v.street_lo, v.cp_lo,
       v.street_hi, v.cp_hi, v.zone, v.score, v.decision, v.guard_veto, v.engine_decided_at,
       v.engine_group_lo, v.engine_group_hi, v.seen_lo, v.seen_hi, v.engine_view, v.agreement
  FROM v
"""
    + _RULINGS_PAIR_WHERE
    + """  AND (%(after_at)s::timestamptz IS NULL
       OR (v.decided_at, v.listing_lo, v.listing_hi)
          < (%(after_at)s::timestamptz, %(after_lo)s::bigint, %(after_hi)s::bigint))
ORDER BY v.decided_at DESC, v.listing_lo DESC, v.listing_hi DESC
LIMIT %(limit)s::int
"""
)

# The counters beside each filter: the total and one count per value of four facets, under the
# CURRENT filters, in one pass over the same rows (GROUPING SETS).
RULING_FACET_COLUMNS: tuple[str, ...] = ("facet", "value", "n")

_RULINGS_FACET_SELECT = """
SELECT CASE WHEN grouping(v.source) = 0 THEN 'source'
            WHEN grouping(v.status) = 0 THEN 'status'
            WHEN grouping(v.verdict_word) = 0 THEN 'verdict'
            WHEN grouping(v.agreement) = 0 THEN 'engine'
            ELSE 'total' END AS facet,
       CASE WHEN grouping(v.source) = 0 THEN v.source
            WHEN grouping(v.status) = 0 THEN v.status
            WHEN grouping(v.verdict_word) = 0 THEN v.verdict_word
            WHEN grouping(v.agreement) = 0 THEN v.agreement END AS value,
       count(*) AS n
  FROM v
"""

_RULINGS_FACET_GROUPING = """
GROUP BY GROUPING SETS ((), (v.source), (v.status), (v.verdict_word), (v.agreement))
"""

RULINGS_PAIR_FACETS_SQL = (
    _RULINGS_PAIR_FROM + _RULINGS_FACET_SELECT + _RULINGS_PAIR_WHERE + _RULINGS_FACET_GROUPING
)

# GROUP GRAIN: the newest ruling of each (group key, pass) with the SET it was taken on (E58),
# and the operator's Browse merges (`operator_merges`, 564) as the groups they are. For each, how
# many properties its adverts sit on now and how the generation groups them. A ruling taken
# before 538 recorded no set (`set_recorded` false): the page offers only its withdrawal.
RULING_GROUP_COLUMNS: tuple[str, ...] = (
    "ruling_key",
    "ruling_id",
    "source",
    "cluster_key",
    "generation",
    "merge_group_id",
    "member_ids",
    "set_recorded",
    "verdict",
    "status",
    "note",
    "reasons",
    "decided_by",
    "decided_at",
    "n_rows",
    "n_members",
    "property_ids",
    "n_properties",
    "together_now",
    "n_engine_groups",
    "n_grouped",
    "n_seen",
    "obec_kod",
    "obec_name",
    "cast_obce_kod",
    "cast_obce_name",
    "engine_view",
    "agreement",
)

_RULINGS_GROUP_FROM = (
    """
WITH cluster_rows AS (
    SELECT x.id, x.cluster_key, x.generation, x.member_ids, x.verdict, x.note, x.reasons,
           x.decided_by, x.decided_at
      FROM autodedup.verdicts x
     WHERE x.kind = 'cluster'
), newest AS (
    SELECT DISTINCT ON (c.cluster_key, coalesce(c.generation, ''::text))
           c.id, c.cluster_key, c.generation, c.member_ids, c.verdict, c.note, c.reasons,
           c.decided_by, c.decided_at
      FROM cluster_rows c
     ORDER BY c.cluster_key, coalesce(c.generation, ''::text), c.decided_at DESC, c.id DESC
), history AS (
    SELECT c.cluster_key, coalesce(c.generation, ''::text) AS pass, count(*) AS n_rows,
           bool_or(c.verdict <> 'unsure') AS ever_stated
      FROM cluster_rows c
     GROUP BY c.cluster_key, coalesce(c.generation, ''::text)
), rulings AS (
    SELECT n.id::text AS ruling_key, n.id AS ruling_id, 'group'::text AS source,
           n.cluster_key, n.generation, NULL::uuid AS merge_group_id,
           coalesce(n.member_ids, '{}'::bigint[]) AS member_ids,
           n.member_ids IS NOT NULL AS set_recorded, n.verdict,
           CASE WHEN n.verdict <> 'unsure' THEN 'standing'
                WHEN h.ever_stated THEN 'withdrawn'
                ELSE 'unsure' END AS status,
           n.note, n.reasons, n.decided_by, n.decided_at, h.n_rows
      FROM newest n
      JOIN history h
        ON h.cluster_key = n.cluster_key AND h.pass = coalesce(n.generation, ''::text)
    UNION ALL
    SELECT m.merge_group_id::text, NULL::bigint, 'browse_merge'::text, NULL::bigint, NULL::text,
           m.merge_group_id, m.member_ids, true, 'same'::text,
           CASE WHEN m.status = 'live' THEN 'standing' ELSE 'withdrawn' END,
           m.status_note, '{}'::text[], m.decided_by, m.merged_at, 1::bigint
      FROM autodedup.operator_merges m
), joined AS (
    SELECT r.*, cardinality(r.member_ids) AS n_members,
           now_on.property_ids, coalesce(now_on.n_properties, 0) AS n_properties,
           (cardinality(r.member_ids) >= 2 AND now_on.n_properties = 1
            AND now_on.n_placed = cardinality(r.member_ids)) AS together_now,
           coalesce(eng.n_engine_groups, 0) AS n_engine_groups,
           coalesce(eng.n_grouped, 0) AS n_grouped,
           coalesce(seen.n_seen, 0) AS n_seen,
           ll.obec_kod, ll.obec_name, ll.cast_obce_kod, ll.cast_obce_name
      FROM rulings r
      LEFT JOIN LATERAL (
          SELECT array_agg(DISTINCT l.property_id)
                     FILTER (WHERE l.property_id IS NOT NULL) AS property_ids,
                 count(DISTINCT l.property_id) AS n_properties,
                 count(l.property_id) AS n_placed
            FROM public.listings l
           WHERE l.id = ANY(r.member_ids)
      ) now_on ON true
      LEFT JOIN LATERAL (
          SELECT count(DISTINCT m.cluster_key) AS n_engine_groups, count(*) AS n_grouped
            FROM autodedup.cluster_members m
           WHERE m.generation = %(generation)s::text AND m.listing_id = ANY(r.member_ids)
      ) eng ON true
      LEFT JOIN LATERAL (
          SELECT count(*) AS n_seen
            FROM unnest(r.member_ids) AS u(listing_id)
           WHERE """
    + _engine_seen("u.listing_id")
    + """
      ) seen ON true
      LEFT JOIN public.listing_location ll ON ll.listing_id = r.member_ids[1]
), w AS (
    SELECT j.*,
           CASE WHEN j.n_members >= 2 AND j.n_grouped = j.n_members AND j.n_engine_groups = 1
                     THEN 'together'
                WHEN j.n_members >= 2 AND j.n_seen = j.n_members THEN 'apart'
                ELSE 'unseen' END AS engine_view
      FROM joined j
), v AS (
    SELECT w.*,
"""
    + _AGREEMENT
    + """
      FROM w
)
"""
)

_RULINGS_GROUP_WHERE = (
    _RULINGS_FILTERS
    + """  AND (%(listing)s::bigint IS NULL OR %(listing)s::bigint = ANY(v.member_ids))
  AND (%(property)s::bigint IS NULL OR %(property)s::bigint = ANY(v.property_ids))
"""
)

RULINGS_GROUP_SQL = (
    _RULINGS_GROUP_FROM
    + """
SELECT v.ruling_key, v.ruling_id, v.source, v.cluster_key, v.generation, v.merge_group_id,
       v.member_ids, v.set_recorded, v.verdict, v.status, v.note, v.reasons, v.decided_by,
       v.decided_at, v.n_rows, v.n_members, v.property_ids, v.n_properties,
       coalesce(v.together_now, false), v.n_engine_groups, v.n_grouped, v.n_seen, v.obec_kod,
       v.obec_name, v.cast_obce_kod, v.cast_obce_name, v.engine_view, v.agreement
  FROM v
"""
    + _RULINGS_GROUP_WHERE
    + """  AND (%(after_at)s::timestamptz IS NULL
       OR (v.decided_at, v.ruling_key) < (%(after_at)s::timestamptz, %(after_key)s::text))
ORDER BY v.decided_at DESC, v.ruling_key DESC
LIMIT %(limit)s::int
"""
)

RULINGS_GROUP_FACETS_SQL = (
    _RULINGS_GROUP_FROM + _RULINGS_FACET_SELECT + _RULINGS_GROUP_WHERE + _RULINGS_FACET_GROUPING
)

# Every ruling of the listed groups' keys, newest first -- the page's expandable history, one
# statement per page (the route keeps the rows of each row's own pass).
GROUP_RULING_HISTORY_SQL = (
    "SELECT"
    + _VERDICT_SELECT_LIST
    + """
FROM autodedup.verdicts v
WHERE v.kind = 'cluster' AND v.cluster_key = any(%(keys)s::bigint[])
ORDER BY v.decided_at DESC, v.id DESC
"""
)

# The town filter's vocabulary: the obce and the quarters (část obce) the rulings touch, named off
# `listing_location`, busiest first and capped -- a code, never typed, as on the queues' block
# picker. Read over the side each filter reads: a pair's low advert, a group's first member.
RULING_TOWN_COLUMNS: tuple[str, ...] = ("grain", "code", "name", "n")

RULING_TOWNS_SQL = """
WITH named AS (
    SELECT x.listing_lo AS listing_id FROM autodedup.verdicts x
     WHERE x.kind = 'pair' AND x.listing_lo IS NOT NULL
    UNION
    SELECT m.listing_lo FROM autodedup.must_not_link m
    UNION
    SELECT x.member_ids[1] FROM autodedup.verdicts x
     WHERE x.kind = 'cluster' AND cardinality(x.member_ids) > 0
    UNION
    SELECT o.member_ids[1] FROM autodedup.operator_merges o
     WHERE cardinality(o.member_ids) > 0
), towns AS (
    SELECT 'o'::text AS grain, ll.obec_kod AS code, max(ll.obec_name) AS name, count(*) AS n
      FROM named d JOIN public.listing_location ll ON ll.listing_id = d.listing_id
     WHERE ll.obec_kod IS NOT NULL
     GROUP BY ll.obec_kod
    UNION ALL
    SELECT 'c'::text, ll.cast_obce_kod, max(ll.cast_obce_name), count(*)
      FROM named d JOIN public.listing_location ll ON ll.listing_id = d.listing_id
     WHERE ll.cast_obce_kod IS NOT NULL
     GROUP BY ll.cast_obce_kod
)
SELECT t.grain, t.code, t.name, t.n
  FROM towns t
 ORDER BY t.n DESC, t.grain DESC, t.code
 LIMIT %(limit)s::int
"""
