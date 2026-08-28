"""Candidate retrieval (migration 450) — per tag, WHICH images to look at next and
WHY each one was drawn.

`tag_candidates` is a review queue and nothing else: membership carries no training
semantics, there is no state column, and an image nobody has reviewed is never a
negative (operator ruling 2026-08-27). Candidates are found, not stumbled on — a
rare tag is a fraction of a percent of the corpus — by ranking a bounded,
category-stratified, per-listing-capped pool against a centroid built ONLY from
that tag's human-verified positives (`source IN ('human','human_confirmed')`;
migration 442's manufactured backfill and unreviewed machine rows are excluded by
predicate, never by deletion). Everything here operates on RANK and PERCENTILE
within one tag's own pool: measured inter-tag centroid cosines span 0.58-0.99, so
an absolute cosine never transfers between tags and no global threshold on
`distance` is ever valid. A tag with fewer than MIN_VERIFIED_POSITIVES verified
positives has no meaningful centroid and is told so instead of being handed a
garbage pool.
"""

from __future__ import annotations

import math
import time
from typing import Any, NamedTuple

import psycopg

from toolkit.tag_definitions import embedding_model
from toolkit.tag_holdout import exclusion_for

# The whole draw vocabulary; mirrored by migration 450's CHECK on tag_candidates.draw.
DRAWS = ("centroid_head", "centroid_mid", "random")

# head: measured precision@100 from a 30k pool is 72-100 percent (8-33x base rate),
# so this is the only band that can build a rare tag's positive set at all — below
# half, one sitting stops producing enough positives to be worth an operator's time;
# above half the set turns prototypical, which is the failure mode a pure top-N has.
# mid: just below the head is where the three measured confusion clusters live
# (bathrooms, circulation, living spaces) — every hard negative the head cannot
# surface comes from here. random: an unranked sample of the WHOLE pool (the head
# included — a sample that skipped the head would not be a base rate), the only
# honest source of one and the only band that can surface a positive the centroid
# is blind to. Sustained positives out of the random band mean the centroid is
# missing a mode, which is why candidate_summary reports each band's yield.
BAND_MIX = {"centroid_head": 0.50, "centroid_mid": 0.30, "random": 0.20}

# The labeled set is 83.8 percent byt against a 43.9 percent corpus, and pozemek is
# under-represented 5.7x. A corpus-proportional draw would stop ADDING to the skew
# but take many sittings to dilute it; a uniform 20 percent would over-weight
# `ostatni` (a small heterogeneous residue) exactly as hard as byt. This mix sits
# between: byt is capped BELOW its corpus share so every sitting actively dilutes
# the skew, and the two thinnest categories get a floor. One dict, one place to
# retune when the composition is re-measured.
CATEGORY_MIX = {"byt": 0.30, "dum": 0.25, "pozemek": 0.20, "komercni": 0.15, "ostatni": 0.10}

# Retrieval quality was MEASURED only at >= 15 positives (median AUC 0.942 over 28
# tags, min 0.859). Below it the quality is unmeasured, and a centroid over fewer
# positives than that is one operator's idiosyncrasies.
MIN_VERIFIED_POSITIVES = 15

DEFAULT_DRAW_COUNT = 120   # one sitting; the grid pages at 200
DRAW_COUNT_MAX = 400       # hard cap on a single request

# Images SCORED per draw, across all categories. Deliberately below the 30,000 of
# the retrieval experiment: that measurement was an index-only scan over consecutive
# ids, while this pool is scattered by design and a vector(512) is 2,056 bytes — over
# TOAST_TUPLE_THRESHOLD — so every vector is a heap fetch plus a TOAST fetch. If draws
# run slower than the budget in production, lower this constant; do NOT "fix" it by
# re-introducing id-consecutive sampling (that samples listings, not images).
POOL_IMAGES_TARGET = 20_000
POOL_IMAGES_PER_LISTING = 4  # 20,000 images => ~5,000 distinct listings, not ~1,400

MID_BAND_PERCENTILE = 0.05   # the mid band is ranks (head, 5 percent of pool]
OVERFETCH = 3                # rows fetched per band = quota x 3, to survive the greedy drops
PER_PROPERTY_CAP = 2         # per (tag, property), counting rows already stored

# Reject when dHash Hamming < 6 — deliberately stricter than the dedup engine's
# l2_phash_hamming_threshold = 11. dHash collapses distinct floor plans (mostly-white
# documents hash alike), and `pudorys` is exactly a tag that needs candidates: a false
# collapse hides a distinct image from review permanently, a false keep costs one click.
NEAR_DUP_MIN_HAMMING = 6

# Bound on EACH arm of the existing-pool read, so the near-duplicate compare list
# is at most twice this. A comparison is ~150ns (measured), so a full list costs
# ~15ms per candidate row and a worst-case category ~18s of pure CPython — which
# is why that pass runs outside the transaction, and the number to retune if a
# tag's queue ever approaches the bound.
PHASH_HISTORY_MAX = 50_000
# TABLESAMPLE reads a fraction of the TABLE, so the fraction needed to fill a pool
# scales inversely with how much of the table the category occupies. Measured
# 2026-08-28 at 3%: byt and dum fill 5,000 listings, pozemek 3,840, komercni 2,930,
# ostatni 401. So the percentage is computed per category from its own row count
# (an index-only scan, ~0.9s) rather than fixed.
#
# The multiple exists because SYSTEM sampling is block-granular and therefore
# lumpy: asking for exactly enough blocks lands short about half the time.
SAMPLE_OVERSAMPLE = 2.5
SAMPLE_PCT_MIN = 1.0     # below this the lumpiness dominates and a draw goes hungry
SAMPLE_PCT_MAX = 100.0   # 100 is a plain seq scan, which is correct for a tiny category

DRAW_STATEMENT_TIMEOUT_MS = 60_000  # ceiling on ONE category's pool query
DRAW_BUDGET_SECONDS = 45     # wall-clock budget for one draw_candidates call (API-safe)
# Below this much budget left, a category is skipped rather than started: the
# per-statement timeout is derived FROM the remaining budget, and a category
# granted a second of it would only burn the operator's wait on a rollback.
DRAW_MIN_CATEGORY_MS = 5_000
DEFAULT_OPEN_TARGET = 200    # runner only: top a tag's open queue up to this


class PoolRow(NamedTuple):
    image_id: int
    listing_id: int
    property_id: int | None
    phash: int | None
    distance: float
    pool_rank: int
    pool_size: int
    draw: str


def property_key(listing_id: int, property_id: int | None) -> str:
    """The per-property cap key. property_id when the listing is attached,
    else 'listing:{listing_id}' — new rows land property_id NULL (rule 19) and a
    bare coalesce onto listing_id could collide with a real property id."""
    return str(property_id) if property_id is not None else f"listing:{listing_id}"


def allocate_counts(total: int, weights: dict[str, float]) -> dict[str, int]:
    """Largest-remainder split that sums to `total` exactly. Deterministic:
    floors first, then the remainder by descending fractional part, ties broken
    by sorted key. Used for BOTH the category split and the band split."""
    if not weights:
        return {}
    if total <= 0:
        return {key: 0 for key in weights}
    raw = {key: total * weight for key, weight in weights.items()}
    out = {key: int(value) for key, value in raw.items()}
    order = sorted(weights, key=lambda key: (-(raw[key] - int(raw[key])), key))
    remainder = total - sum(out.values())
    index = 0
    while remainder > 0:
        out[order[index % len(order)]] += 1
        remainder -= 1
        index += 1
    # Only reachable when the weights sum above 1; peel back from the smallest
    # fractional part so the returned split still sums to `total` exactly.
    while remainder < 0:
        key = order[-1 - (index % len(order))]
        if out[key] > 0:
            out[key] -= 1
            remainder += 1
        index += 1
    return out


# --- reads ------------------------------------------------------------------

_TAG_EXISTS_SQL = "SELECT 1 FROM tag_taxonomy WHERE id = %(tag_id)s"

_TAG_ROUTING_SQL = """
    SELECT routing_categories FROM tag_taxonomy WHERE id = %(tag_id)s
"""


def routing_categories(
    conn: psycopg.Connection, *, tag_id: int,
) -> tuple[str, ...]:
    """The property types this tag serves (migration 457), or () for no scope.

    Unknown values are dropped rather than raised on: the column is operator-owned
    and a typo there should narrow a draw, never break one. An array that survives
    filtering to nothing is treated as no scope, so a tag can't be left undrawable
    by a bad edit."""
    with conn.cursor() as cur:
        cur.execute(_TAG_ROUTING_SQL, {"tag_id": tag_id})
        row = cur.fetchone()
    stored = (row[0] if row else None) or []
    return tuple(c for c in stored if c in CATEGORY_MIX)


def scoped_mix(scope: tuple[str, ...]) -> dict[str, float]:
    """CATEGORY_MIX restricted to `scope` and renormalised to sum to 1.

    Renormalised, not merely filtered: filtering alone would leave the weights
    summing to <1 and allocate_counts would hand back fewer rows than asked for —
    the short draw would look like a thin pool instead of a narrowed scope."""
    if not scope:
        return dict(CATEGORY_MIX)
    total = sum(CATEGORY_MIX[c] for c in scope)
    return {c: CATEGORY_MIX[c] / total for c in scope}

# The predicate is byte-identical to the centroid CTE's in _DRAW_POOL_SQL. If the
# floor check and the centroid ever disagreed about the population, the floor would
# be a lie — a tag could pass the gate and still produce an empty pool.
_COUNT_VERIFIED_POSITIVES_SQL = f"""
    SELECT count(*)::int
    FROM image_tag_labels itl
    JOIN image_clip_embeddings e
      ON e.image_id = itl.image_id AND e.model = %(model)s::text
    WHERE itl.tag_id = %(tag_id)s
      AND itl.state = 'positive'
      AND itl.source IN ('human', 'human_confirmed')
      {exclusion_for("itl")}
"""

# TWO arms, because the near-duplicate rail is about the training set and the
# training set is not only the queue. Arm one is this tag's stored candidates;
# arm two is every image already DECIDED for the tag — the 1,440 human positives
# predate this table entirely, so without it the whole of today's ground truth is
# invisible to the check and a byte-identical twin of a stored positive can be
# queued, labeled again, and inflate the head. It mirrors the pool query's own
# `NOT EXISTS ... image_tag_labels` exclusion, which keys on image_id and so
# cannot see a twin under a different id.
#
# Bounded on purpose, per arm: past %(limit)s rows a near-duplicate of one of the
# OLDEST candidates can slip through, which costs one review click where an
# unbounded scan costs a request. Arm two orders human decisions FIRST so the
# bound sheds migration 442's manufactured rows before it sheds a real one.
_EXISTING_POOL_SQL = """
    SELECT q.image_id, q.phash, q.listing_id, q.property_id
    FROM (
      (
        SELECT c.image_id, c.phash, c.listing_id, c.property_id
        FROM tag_candidates c
        WHERE c.tag_id = %(tag_id)s
        ORDER BY c.drawn_at DESC, c.image_id DESC
        LIMIT %(limit)s
      )
      UNION ALL
      (
        SELECT i.id AS image_id, i.phash, i.listing_id, l.property_id
        FROM image_tag_labels itl
        JOIN images i ON i.id = itl.image_id
        LEFT JOIN listings l ON l.id = i.listing_id
        WHERE itl.tag_id = %(tag_id)s
        ORDER BY (itl.source IN ('human', 'human_confirmed')) DESC, itl.image_id DESC
        LIMIT %(limit)s
      )
    ) q
"""

# `positive` / `negative` are the per-band YIELD, and the random band's is the
# one self-check this design has: an unranked sample of the pool that keeps
# coming back positive means the centroid is missing a mode. A band readout
# without it can only say how much work is left, never whether the retrieval is
# working.
_SUMMARY_BY_DRAW_SQL = """
    SELECT c.draw,
           count(*)::int AS total,
           count(*) FILTER (WHERE lab.image_id IS NULL)::int AS open,
           count(*) FILTER (WHERE lab.state = 'positive')::int AS positive,
           count(*) FILTER (WHERE lab.state = 'negative')::int AS negative,
           max(c.drawn_at) AS last_drawn_at
    FROM tag_candidates c
    LEFT JOIN image_tag_labels lab
      ON lab.image_id = c.image_id AND lab.tag_id = c.tag_id
    WHERE c.tag_id = %(tag_id)s
    GROUP BY c.draw
"""

_SUMMARY_BY_CATEGORY_SQL = """
    SELECT c.category_main,
           count(*)::int AS total,
           count(*) FILTER (WHERE lab.image_id IS NULL)::int AS open,
           count(*) FILTER (WHERE lab.state = 'positive')::int AS positive,
           count(*) FILTER (WHERE lab.state = 'negative')::int AS negative,
           max(c.drawn_at) AS last_drawn_at
    FROM tag_candidates c
    LEFT JOIN image_tag_labels lab
      ON lab.image_id = c.image_id AND lab.tag_id = c.tag_id
    WHERE c.tag_id = %(tag_id)s
    GROUP BY c.category_main
"""


def _tag_exists(conn: psycopg.Connection, *, tag_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(_TAG_EXISTS_SQL, {"tag_id": tag_id})
        return cur.fetchone() is not None


def count_verified_positives(
    conn: psycopg.Connection, *, tag_id: int, model: str | None = None,
) -> int:
    """How many of this tag's positives a HUMAN decided AND have a CLIP vector —
    the centroid's actual population, which is not the overview's positive_count
    (that one still includes the migration-442 backfill)."""
    with conn.cursor() as cur:
        cur.execute(
            _COUNT_VERIFIED_POSITIVES_SQL,
            {"tag_id": tag_id, "model": model or embedding_model()},
        )
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


# --- the pool ---------------------------------------------------------------

# Sampling by CONSECUTIVE image_id samples LISTINGS, not images (measured: 30,000
# consecutive ids came from 2,106 listings, 14.25 per listing), so the pool is built
# the other way round — a listing lottery, then an image lottery per drawn listing.
# The inner ORDER BY is random(), never `sequence`: a listing's first photos are
# systematically the exterior/living-room hero shots and floor plans sit at the end,
# so ordering by sequence would make `pudorys` structurally unreachable.
# TABLESAMPLE SYSTEM was rejected — it samples PAGES, and pages are insert-time
# clusters, which is the same trap wearing a different hat.
#
# `ranked` and `centroid` are each referenced more than once, so Postgres
# materialises them: the pool is scored exactly once. Every bound parameter carries
# an explicit cast so tests/test_sql_schema_prepare.py can type it without values.
_DRAW_POOL_SQL = f"""
    WITH centroid AS (
      SELECT avg(e.embedding) AS vec, count(*)::int AS positives
      FROM image_tag_labels itl
      JOIN image_clip_embeddings e
        ON e.image_id = itl.image_id AND e.model = %(model)s::text
      WHERE itl.tag_id = %(tag_id)s
        AND itl.state = 'positive'
        AND itl.source IN ('human', 'human_confirmed')
        {exclusion_for("itl")}
      HAVING count(*) >= %(min_positives)s::bigint
    ),
    pool_listings AS (
      SELECT l.id, l.property_id
      FROM listings l TABLESAMPLE SYSTEM (%(sample_pct)s)
      WHERE l.category_main = %(category_main)s::text
      LIMIT %(pool_listings)s
    ),
    pool AS (
      SELECT im.image_id, im.phash, im.listing_id, pl.property_id
      FROM pool_listings pl
      JOIN LATERAL (
        SELECT i.id AS image_id, i.phash, i.listing_id
        FROM images i
        WHERE i.listing_id = pl.id
          AND i.storage_path IS NOT NULL
        ORDER BY random()
        LIMIT %(images_per_listing)s
      ) im ON true
    ),
    scored AS (
      SELECT p.image_id, p.phash, p.listing_id, p.property_id,
             (e.embedding <=> c.vec) AS distance
      FROM pool p
      JOIN image_clip_embeddings e
        ON e.image_id = p.image_id AND e.model = %(model)s::text
      CROSS JOIN centroid c
      WHERE NOT EXISTS (
              SELECT 1 FROM image_tag_labels a
              WHERE a.image_id = p.image_id AND a.tag_id = %(tag_id)s
            )
        AND NOT EXISTS (
              SELECT 1 FROM tag_candidates tc
              WHERE tc.tag_id = %(tag_id)s AND tc.image_id = p.image_id
            )
        {exclusion_for("p")}
    ),
    collapsed AS (
      SELECT x.image_id, x.phash, x.listing_id, x.property_id, x.distance
      FROM (
        SELECT s.*,
               row_number() OVER (
                 PARTITION BY s.phash ORDER BY s.distance, s.image_id
               ) AS phash_rn
        FROM scored s
      ) x
      WHERE x.phash IS NULL OR x.phash_rn = 1
    ),
    ranked AS (
      SELECT c.image_id, c.phash, c.listing_id, c.property_id, c.distance,
             (row_number() OVER (ORDER BY c.distance, c.image_id))::int AS pool_rank,
             (count(*) OVER ())::int AS pool_size
      FROM collapsed c
    ),
    head AS (
      SELECT r.*, 'centroid_head'::text AS draw, r.pool_rank AS band_ord
      FROM ranked r
      WHERE r.pool_rank <= %(head_fetch)s::int
    ),
    -- The upper bound never falls below the band's own fetch window. It is a
    -- PERCENTILE of the pool while the lower bound is the head's OVERFETCHED
    -- window, so on a thin pool the head window can overrun the percentile and
    -- the predicate becomes `rank > H and rank <= M` with H > M -- a band that is
    -- empty by arithmetic, silently costing every hard negative near the three
    -- confusion clusters. Degrading to "the mid_fetch ranks just below the head"
    -- is the honest floor: still just below the head, which is the band's whole
    -- definition.
    mid AS (
      SELECT m.*, (row_number() OVER (ORDER BY random()))::int AS band_ord
      FROM (
        SELECT r.*, 'centroid_mid'::text AS draw
        FROM ranked r
        WHERE r.pool_rank > %(head_fetch)s::int
          AND r.pool_rank <= greatest(
                ceil(r.pool_size * %(mid_percentile)s::double precision),
                %(mid_floor)s::double precision)
        ORDER BY random()
        LIMIT %(mid_fetch)s
      ) m
    ),
    -- The WHOLE pool, head included. Excluding the head would make this a sample
    -- of the pool minus its highest-yield region, which is not a base rate; the
    -- overlap is resolved in select_candidates, where the head is consumed first
    -- and a repeat image_id is skipped.
    rnd AS (
      SELECT n.*, (row_number() OVER (ORDER BY random()))::int AS band_ord
      FROM (
        SELECT r.*, 'random'::text AS draw
        FROM ranked r
        ORDER BY random()
        LIMIT %(random_fetch)s
      ) n
    )
    SELECT u.image_id, u.listing_id, u.property_id, u.phash,
           u.distance, u.pool_rank, u.pool_size, u.draw,
           (SELECT positives FROM centroid) AS centroid_positives
    FROM (
      SELECT * FROM head
      UNION ALL SELECT * FROM mid
      UNION ALL SELECT * FROM rnd
    ) u
    -- band_ord, NOT pool_rank. select_candidates walks this order greedily and
    -- stops each band at its quota, so ordering the whole union by similarity
    -- would hand the mid and random bands the nearest THIRD of their overfetch
    -- and nothing else -- the tail would be structurally unreachable and the
    -- random band's base rate biased high. band_ord is the rank inside the head
    -- and a shuffle inside the two sampled bands, which is what each band means.
    ORDER BY
      CASE u.draw WHEN 'centroid_head' THEN 0 WHEN 'centroid_mid' THEN 1 ELSE 2 END,
      u.band_ord
"""

# definition_id is resolved HERE, from the row's own tag, exactly as migration 446's
# annotation upsert does — never a parameter, so a caller can never cite another
# tag's definition. Served by tag_definitions_one_active_idx.
_INSERT_CANDIDATE_SQL = """
    INSERT INTO tag_candidates (
      tag_id, image_id, draw, category_main, distance, pool_rank, pool_size,
      listing_id, property_id, phash, centroid_positive_count, model,
      definition_id, drawn_by
    )
    VALUES (
      %(tag_id)s, %(image_id)s, %(draw)s, %(category_main)s, %(distance)s,
      %(pool_rank)s, %(pool_size)s, %(listing_id)s, %(property_id)s, %(phash)s,
      %(centroid_positive_count)s, %(model)s,
      (SELECT id FROM tag_definitions
        WHERE tag_id = %(tag_id)s AND status = 'active'),
      %(drawn_by)s
    )
    ON CONFLICT (tag_id, image_id) DO NOTHING
"""


def select_candidates(
    rows: list[PoolRow], *, quotas: dict[str, int],
    existing_phashes: list[int], existing_property_counts: dict[str, int],
    per_property_cap: int = PER_PROPERTY_CAP,
    min_hamming: int = NEAR_DUP_MIN_HAMMING,
) -> tuple[list[PoolRow], dict[str, int]]:
    """Returns (accepted rows, {'near_dup': n, 'property_cap': n}). Pure — no DB,
    no clock, no randomness. This is where the selection POLICY lives, so it is
    unit-testable without faking cosine.

    Pairwise Hamming and running per-group counters are inherently sequential;
    expressing them in SQL needs a recursive CTE nobody can read or bound, and the
    input is already bounded to count x OVERFETCH rows."""
    # Lazy, like scraper/main.py's: scraper.image_phash imports Pillow at module
    # level, and the API process has no business loading it to count bits. ONE
    # hamming() in the repo — never a second implementation.
    from scraper.image_phash import hamming

    counts = dict(existing_property_counts)
    compare = list(existing_phashes)
    taken = {band: 0 for band in quotas}
    accepted: list[PoolRow] = []
    seen: set[int] = set()
    dropped = {"near_dup": 0, "property_cap": 0}
    for row in rows:
        # The bands overlap by construction — random samples the WHOLE pool, so it
        # can re-draw a head or mid row. First occurrence wins and the row keeps
        # the band it was accepted under; the head is walked first, so a shared row
        # is credited to the band that would have taken it anyway.
        if row.image_id in seen:
            continue
        # A met quota is the normal stop condition, not a loss — not reported.
        if taken.get(row.draw, 0) >= quotas.get(row.draw, 0):
            continue
        key = property_key(row.listing_id, row.property_id)
        if counts.get(key, 0) >= per_property_cap:
            dropped["property_cap"] += 1
            continue
        # An image with no phash skips this check entirely and is caught only by the
        # per-property cap. phash catches re-encodes and cross-agency reuse; the same
        # room from a different angle is the cap's job, not the hash's.
        if row.phash is not None and any(
            hamming(row.phash, other) < min_hamming for other in compare
        ):
            dropped["near_dup"] += 1
            continue
        accepted.append(row)
        seen.add(row.image_id)
        counts[key] = counts.get(key, 0) + 1
        taken[row.draw] = taken.get(row.draw, 0) + 1
        if row.phash is not None:
            compare.append(row.phash)
    return accepted, dropped


def _existing_pool(
    conn: psycopg.Connection, *, tag_id: int, limit: int = PHASH_HISTORY_MAX,
) -> tuple[list[int], dict[str, int]]:
    """Phashes and per-property counts for everything this tag has already queued
    OR already decided, in one bounded read. An image the operator decided months
    ago still occupies its property's cap and still blocks its own twin."""
    with conn.cursor() as cur:
        cur.execute(_EXISTING_POOL_SQL, {"tag_id": tag_id, "limit": limit})
        rows = cur.fetchall()
    # An image can be BOTH a stored candidate and a decided label; counting it
    # twice would halve that property's real cap.
    seen: set[int] = set()
    phashes: list[int] = []
    counts: dict[str, int] = {}
    for r in rows:
        if r[0] in seen:
            continue
        seen.add(r[0])
        if r[1] is not None:
            phashes.append(int(r[1]))
        key = property_key(r[2], r[3])
        counts[key] = counts.get(key, 0) + 1
    return phashes, counts


_CATEGORY_COUNT_SQL = """
    SELECT count(*)::bigint FROM listings WHERE category_main = %(category_main)s::text
"""


def category_listing_count(conn: psycopg.Connection, *, category_main: str) -> int:
    """How many listings this category holds. An index-only scan on
    listings_category_main_category_type_idx — ~0.9s for byt, against the ~35s the
    ORDER BY random() pool build used to cost, so paying it to size the sample is
    a bargain rather than an extra."""
    with conn.cursor() as cur:
        cur.execute(_CATEGORY_COUNT_SQL, {"category_main": category_main})
        row = cur.fetchone()
    return int(row[0]) if row else 0


def sample_pct(*, pool_listings: int, category_count: int) -> float:
    """The TABLESAMPLE percentage that should yield `pool_listings` rows.

    TABLESAMPLE reads a fraction of the whole TABLE, and the filter runs after, so
    the fraction has to be scaled by how much of the table this category occupies.
    An unknown or empty category falls back to a full scan rather than sampling
    nothing — a draw that returns zero because of an arithmetic edge would look
    exactly like a thin corpus."""
    if category_count <= 0:
        return SAMPLE_PCT_MAX
    wanted = 100.0 * pool_listings * SAMPLE_OVERSAMPLE / category_count
    return min(SAMPLE_PCT_MAX, max(SAMPLE_PCT_MIN, wanted))


def _draw_pool_params(
    *, tag_id: int, model: str, category_main: str, band_quotas: dict[str, int],
    scoped: bool, category_count: int,
) -> dict[str, Any]:
    # A draw PINNED to one category gets the whole pool budget, because the whole
    # count was allocated to it. Sizing the pool by that category's share of the
    # mix would shrink the pool while the quota — and with it the head's fetch
    # window — grew, which is how the mid band gets squeezed out from below.
    share = 1.0 if scoped else CATEGORY_MIX[category_main]
    pool_images = round(POOL_IMAGES_TARGET * share)
    head_fetch = band_quotas["centroid_head"] * OVERFETCH
    mid_fetch = band_quotas["centroid_mid"] * OVERFETCH
    pool_listings = math.ceil(pool_images / POOL_IMAGES_PER_LISTING)
    return {
        "tag_id": tag_id,
        "model": model,
        "min_positives": MIN_VERIFIED_POSITIVES,
        "category_main": category_main,
        "sample_pct": sample_pct(
            pool_listings=pool_listings, category_count=category_count,
        ),
        "pool_listings": pool_listings,
        "images_per_listing": POOL_IMAGES_PER_LISTING,
        "head_fetch": head_fetch,
        "mid_percentile": MID_BAND_PERCENTILE,
        "mid_fetch": mid_fetch,
        # The floor under the mid band's percentile bound: the band can never
        # close below its own fetch window, so it is never empty by arithmetic.
        "mid_floor": head_fetch + mid_fetch,
        "random_fetch": band_quotas["random"] * OVERFETCH,
    }


def _insert_params(
    row: PoolRow, *, tag_id: int, category_main: str, centroid_positives: int,
    model: str, drawn_by: str,
) -> dict[str, Any]:
    return {
        "tag_id": tag_id, "image_id": row.image_id, "draw": row.draw,
        "category_main": category_main, "distance": row.distance,
        "pool_rank": row.pool_rank, "pool_size": row.pool_size,
        "listing_id": row.listing_id, "property_id": row.property_id,
        "phash": row.phash, "centroid_positive_count": centroid_positives,
        "model": model, "drawn_by": drawn_by,
    }


def _draw_one_category(
    conn: psycopg.Connection, *, tag_id: int, category_main: str, quota: int,
    model: str, drawn_by: str, existing_phashes: list[int],
    existing_property_counts: dict[str, int], scoped: bool,
    timeout_ms: int = DRAW_STATEMENT_TIMEOUT_MS,
) -> tuple[list[PoolRow], dict[str, Any]]:
    """One category's pool: score, then select, then insert. Each SQL step is its
    own transaction — scraper.db.connect() is autocommit, so SET LOCAL needs one,
    and a cancelled statement leaves nothing half-written. The near-duplicate pass
    runs BETWEEN them on purpose: it is pure CPython over a bounded compare list
    and holding a transaction open across it buys nothing."""
    band_quotas = allocate_counts(quota, BAND_MIX)
    started = time.monotonic()
    # Read OUTSIDE the timed transaction: it sizes the sample, so charging it to
    # the pool query's own ceiling would make a slow count eat the budget it exists
    # to protect.
    category_count = category_listing_count(conn, category_main=category_main)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
        cur.execute(
            _DRAW_POOL_SQL,
            _draw_pool_params(
                tag_id=tag_id, model=model, category_main=category_main,
                band_quotas=band_quotas, scoped=scoped,
                category_count=category_count,
            ),
        )
        raw = cur.fetchall()
    rows = [PoolRow(*r[:8]) for r in raw]
    centroid_positives = int(raw[0][8]) if raw and raw[0][8] is not None else 0
    accepted, dropped = select_candidates(
        rows, quotas=band_quotas, existing_phashes=existing_phashes,
        existing_property_counts=existing_property_counts,
    )
    if accepted:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
            cur.executemany(
                _INSERT_CANDIDATE_SQL,
                [
                    _insert_params(
                        row, tag_id=tag_id, category_main=category_main,
                        centroid_positives=centroid_positives, model=model,
                        drawn_by=drawn_by,
                    )
                    for row in accepted
                ],
            )
    report = {
        "category_main": category_main,
        # pool_size is the pool AFTER exclusions and exact-hash collapse — not the
        # corpus, and not POOL_IMAGES_TARGET.
        "status": "drawn" if rows else "empty_pool",
        "requested": quota,
        "pool_size": rows[0].pool_size if rows else 0,
        "inserted": len(accepted),
        "dropped_near_dup": dropped["near_dup"],
        "dropped_property_cap": dropped["property_cap"],
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    return accepted, report


def _empty_draw_report(
    *, tag_id: int, count: int, verified: int, model: str,
) -> dict[str, Any]:
    return {
        "tag_id": tag_id, "status": "insufficient_positives",
        "requested": count, "inserted": 0,
        "verified_positive_count": verified,
        "min_verified_positives": MIN_VERIFIED_POSITIVES,
        "model": model,
        "by_draw": {band: 0 for band in DRAWS},
        "by_category": {},
        "dropped_near_dup": 0, "dropped_property_cap": 0,
        "categories": [],
    }


def draw_candidates(
    conn: psycopg.Connection, *, tag_id: int, count: int = DEFAULT_DRAW_COUNT,
    category_main: str | None = None, drawn_by: str = "operator",
    model: str | None = None, max_seconds: int = DRAW_BUDGET_SECONDS,
) -> dict[str, Any]:
    """Draw `count` candidates for one tag by centroid retrieval, category by
    category, each bounded by its own statement timeout.

    A tag with too few human-verified positives RETURNS status
    'insufficient_positives' and writes nothing — never a silently empty pool, and
    never a centroid over a population nobody measured. A cancelled category
    (statement timeout) or an exhausted wall-clock budget degrades that category
    alone; the call still returns a report."""
    if not _tag_exists(conn, tag_id=tag_id):
        raise KeyError(tag_id)
    if not 1 <= int(count) <= DRAW_COUNT_MAX:
        raise ValueError(f"count must be between 1 and {DRAW_COUNT_MAX}")
    if category_main is not None and category_main not in CATEGORY_MIX:
        raise ValueError(f"unknown category_main {category_main!r}")

    model = model or embedding_model()
    verified = count_verified_positives(conn, tag_id=tag_id, model=model)
    if verified < MIN_VERIFIED_POSITIVES:
        return _empty_draw_report(
            tag_id=tag_id, count=count, verified=verified, model=model,
        )

    # A tag's routing scope (migration 457) narrows the mix to the property types it
    # actually serves. Without it the fixed mix sent 44% of a bathroom draw at pozemek
    # and ostatni, where bathrooms essentially do not occur — measured on the first
    # live koupelna draw: 54 rows, pozemek 24 / komercni 18 / ostatni 12, byt 0, dum 0.
    # An explicit category_main still wins: it is the caller asking for one category.
    scope = () if category_main is not None else routing_categories(conn, tag_id=tag_id)
    allocation = (
        allocate_counts(count, scoped_mix(scope)) if category_main is None
        else {category_main: count}
    )
    existing_phashes, existing_property_counts = _existing_pool(conn, tag_id=tag_id)

    by_draw = {band: 0 for band in DRAWS}
    by_category: dict[str, int] = {}
    reports: list[dict[str, Any]] = []
    started = time.monotonic()
    exhausted = False
    # SMALLEST QUOTA FIRST, because the loop order IS the degradation policy:
    # whatever the budget cuts is cut from the END. CATEGORY_MIX order would always
    # sacrifice komercni and ostatni — the two thinnest, the two the mix gives a
    # floor to — while guaranteeing byt, the category capped BELOW its corpus share
    # precisely to dilute the labeled set's 83.8% byt skew. category_main is stored
    # per row, so that drift would be durable in the table, not merely momentary.
    ordered = [kv for kv in sorted(allocation.items(), key=lambda kv: (kv[1], kv[0]))
               if kv[1] > 0]
    for position, (category, quota) in enumerate(ordered):
        # The per-statement ceiling is derived from what is LEFT of the budget,
        # never a constant: a fixed 60s timeout on a category that starts at 44.9s
        # of a 45s budget lets one synchronous admin request run ~105s and die at
        # the proxy on top of committed work.
        #
        # FAIR SHARE, not greedy. Handing each category the whole remaining budget
        # as its ceiling lets an early one eat it and leave the rest 'skipped_budget'
        # — and since the loop runs smallest-quota-first, the starved ones are always
        # byt and dum, the two categories most tags care about most. Dividing by the
        # categories still to come bounds that, while recomputing it each iteration
        # rolls unused time forward, so the last category (the largest, deliberately)
        # still inherits whatever the small ones did not spend.
        timeout_ms = DRAW_STATEMENT_TIMEOUT_MS
        if max_seconds > 0:
            remaining_ms = int((max_seconds - (time.monotonic() - started)) * 1000)
            share_ms = remaining_ms // max(1, len(ordered) - position)
            # Fair share only while a fair share is workable. Once it would fall
            # below the per-category floor, sharing it out would put EVERY category
            # under the floor and skip all of them — so at that point spend what is
            # left on this one category instead of wasting the remainder entirely.
            budget_ms = share_ms if share_ms >= DRAW_MIN_CATEGORY_MS else remaining_ms
            timeout_ms = min(DRAW_STATEMENT_TIMEOUT_MS, budget_ms)
        if exhausted or timeout_ms < DRAW_MIN_CATEGORY_MS:
            exhausted = True
            reports.append({
                "category_main": category, "status": "skipped_budget",
                "requested": quota, "pool_size": 0, "inserted": 0,
                "dropped_near_dup": 0, "dropped_property_cap": 0, "elapsed_ms": 0,
            })
            by_category[category] = 0
            continue
        try:
            accepted, report = _draw_one_category(
                conn, tag_id=tag_id, category_main=category, quota=quota, model=model,
                drawn_by=drawn_by, existing_phashes=existing_phashes,
                existing_property_counts=existing_property_counts,
                scoped=category_main is not None, timeout_ms=timeout_ms,
            )
        except psycopg.errors.QueryCanceled:
            # The transaction rolled back; this category lands nothing and the rest
            # of the draw carries on.
            reports.append({
                "category_main": category, "status": "timeout", "requested": quota,
                "pool_size": 0, "inserted": 0, "dropped_near_dup": 0,
                "dropped_property_cap": 0,
                "elapsed_ms": timeout_ms,
            })
            by_category[category] = 0
            continue
        # Fold what landed back into the running state so the NEXT category cannot
        # re-introduce a near-duplicate or blow the per-property cap.
        for row in accepted:
            if row.phash is not None:
                existing_phashes.append(row.phash)
            key = property_key(row.listing_id, row.property_id)
            existing_property_counts[key] = existing_property_counts.get(key, 0) + 1
            by_draw[row.draw] = by_draw.get(row.draw, 0) + 1
        by_category[category] = report["inserted"]
        reports.append(report)

    return {
        "tag_id": tag_id, "status": "drawn",
        "requested": count,
        "inserted": sum(r["inserted"] for r in reports),
        "verified_positive_count": verified,
        "min_verified_positives": MIN_VERIFIED_POSITIVES,
        "model": model,
        "by_draw": by_draw,
        "by_category": by_category,
        "dropped_near_dup": sum(r["dropped_near_dup"] for r in reports),
        "dropped_property_cap": sum(r["dropped_property_cap"] for r in reports),
        "categories": reports,
    }


def _bucket_order(keys: list[str], preferred: tuple[str, ...]) -> list[str]:
    """Preferred vocabulary first, in ITS order; anything else (category_main is
    free text) sorted after it."""
    known = [k for k in preferred if k in keys]
    return known + sorted(k for k in keys if k not in preferred)


def candidate_summary(
    conn: psycopg.Connection, *, tag_id: int, model: str | None = None,
) -> dict[str, Any]:
    """This tag's review queue: how big it is, how much of it is still undecided,
    how it was drawn (rank band and category quota), what each bucket YIELDED, and
    whether the tag has enough human-verified positives to draw more.

    `open` / `positive` / `negative` are all derived by joining image_tag_labels —
    the queue itself stores no state, and reading a yield off it is the only way
    to answer "is this centroid missing a mode" without hand-counting a grid."""
    if not _tag_exists(conn, tag_id=tag_id):
        raise KeyError(tag_id)
    model = model or embedding_model()
    with conn.cursor() as cur:
        cur.execute(_SUMMARY_BY_DRAW_SQL, {"tag_id": tag_id})
        draw_rows = cur.fetchall()
        cur.execute(_SUMMARY_BY_CATEGORY_SQL, {"tag_id": tag_id})
        category_rows = cur.fetchall()
    verified = count_verified_positives(conn, tag_id=tag_id, model=model)

    def _bucket(row: tuple[Any, ...]) -> dict[str, int]:
        return {"total": row[1], "open": row[2], "positive": row[3], "negative": row[4]}

    by_draw = {r[0]: _bucket(r) for r in draw_rows}
    by_category = {r[0]: _bucket(r) for r in category_rows}
    last_drawn_at = max((r[5] for r in draw_rows if r[5] is not None), default=None)
    total = sum(v["total"] for v in by_draw.values())
    open_count = sum(v["open"] for v in by_draw.values())
    return {
        "tag_id": tag_id,
        "total": total, "open": open_count, "reviewed": total - open_count,
        "last_drawn_at": last_drawn_at,
        "verified_positive_count": verified,
        "min_verified_positives": MIN_VERIFIED_POSITIVES,
        "can_draw": verified >= MIN_VERIFIED_POSITIVES,
        "model": model,
        # Rendered, never recomputed in the SPA: a draw that quietly covers three of
        # five property types must SAY so, or a short draw reads as a thin corpus.
        "routing_categories": list(routing_categories(conn, tag_id=tag_id)),
        # Empty buckets are omitted, never zero-filled: a band that never produced a
        # row and a band that produced only decided rows are different facts.
        "by_draw": [
            {"key": key, **by_draw[key]} for key in _bucket_order(list(by_draw), DRAWS)
        ],
        "by_category": [
            {"key": key, **by_category[key]}
            for key in _bucket_order(list(by_category), tuple(CATEGORY_MIX))
        ],
    }
