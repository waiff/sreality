"""Build the DINOv3 bake-off manifest — the five §5.2 populations, zero-credential.

Runs in GitHub Actions (`.github/workflows/dinov3_bakeoff_manifest.yml`), where
SUPABASE_DB_URL and the R2_* secrets live. READ-ONLY against Postgres (SELECT only)
and presign-only against R2: it writes nothing to any table, least of all
`image_clip_embeddings` or `image_dinov3_embeddings`.

WHAT IT EMITS. One JSON manifest carrying, for every image it draws, a 7-day
presigned R2 GET URL (so the pod that consumes it needs NO credentials), the stored
`phash`, the stored `render_score`, and — per PAIR — the stored-CLIP cosine computed
in SQL with pgvector's `<=>`. That last one is the incumbent's arm scored at zero GPU
cost on byte-identical pairs, which is the only way the comparison is fair.

THE FIVE POPULATIONS (docs/design/new-dedup/ENCODER-DECISION.md §5.2):

  P1a  images of DIFFERENT listings whose pHash Hamming distance is <= --p1a-hamming
       (default 2). The CONTROL: the reposts the cheap incumbent signal already
       finds. Matched in Python via a pigeonhole chunk index, never a full-table
       self-join — over a block-sampled pool by default (cheap, a recall floor) or
       corpus-wide exact-pHash collision groups under `--p1a-mode full`.
  P1b  a sample of real corpus photos. The pod applies the synthetic transforms
       listed in `transform_recipes` to each one and scores cosine(x, T(x)). THE
       headline population: the pHash-breaking same-photo case the embedding tier
       exists for. Only the recipe list ships here; no transformed bytes.
  P2   two images of the SAME listing carrying the same tag. Hard negatives for
       photo identity; legitimately contains burst shots.
  P3   two images carrying the same tag whose listings sit in DIFFERENT obce. The
       modal failure mode; separation between P1b and P3 is the headline readout.
  P4   document-tagged images from different listings. INSPECTION ONLY, never a
       scored positive set: dHash collapses distinct floor plans (mostly-white
       documents hash alike — toolkit/tag_candidates.py), so a low-Hamming document
       pair is not evidence of a repost.

Plus a WEIGHTS CANARY fixture: a small, deterministic handful of image ids (lowest
ids with stored bytes) used only to check the gated `facebook/dinov3-vitb16-…`
weights against the ungated `timm/…` mirror. Deterministic by construction, so it is
stable across runs without anybody having to write ids down.

THE SEALED EXAM IS EXCLUDED. Every read of `image_tag_labels` formats
`toolkit.tag_holdout.exclusion_for(...)` in — there is no legitimate reason to want
exam images in a bake-off population, and `tests/test_holdout_exclusion_census.py`
is the rail that keeps it that way.

SCOPE CUT, STATED: this script and its workflow build a MANIFEST. Running the
pod-side harness (`scripts/dinov3_bakeoff.py`) against it on a real GPU pod is a
MANUAL step the operator performs by hand when ready to spend the ~$2 §5.3
estimates. Nothing here launches a pod, and nothing here spends money.

Run: `python -m scripts.dinov3_bakeoff_manifest --out dinov3_manifest.json`
(env: SUPABASE_DB_URL + R2_*; `--dry-run` needs only the database).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from toolkit import tag_holdout

LOG = logging.getLogger("dinov3_bakeoff_manifest")

# The stored-CLIP arm's join key. `image_clip_embeddings` is keyed (image_id, model),
# so the model NAME is what selects the incumbent's vectors; data/clip_taxonomy.json
# is where the pinned name and revision live (migration 456).
_TAXONOMY_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "data", "clip_taxonomy.json")
_FALLBACK_CLIP_MODEL = "openai/clip-vit-base-patch32"

# P4's document tags. tag_taxonomy.label is operator-curated Czech free text
# ("interier - koupelna", "exterier - parkoviště"), so these are ILIKE PATTERNS, not
# exact labels, and --dry-run prints which labels each one actually matched. If a
# pattern matches nothing the operator fixes the flag, not the code.
DEFAULT_DOCUMENT_TAG_PATTERNS = ["%pudorys%", "%půdorys%", "%katastr%", "%plan%", "%plán%"]

# The CLIP logical-tag vocabulary (data/clip_taxonomy.json `collapse`) is a DIFFERENT
# vocabulary from tag_taxonomy's operator-curated labels. These three are its
# document-like classes, and P1b skips them: a synthetic crop of a floor plan measures
# nothing useful, and §5.2 says document positives come only from P1b's photo arm.
CLIP_DOCUMENT_TAGS = ["floor_plan", "site_plan", "property_document"]

# The synthetic transforms the POD applies to P1b (ENCODER-DECISION §5.2). Only the
# recipe travels — the pod owns the pixels. `watermark_band` is deliberately placed
# in the left/right band, not the centre: centre-cropping preprocessing would hide a
# centred watermark, and whether an encoder survives an EDGE watermark is exactly
# what the preprocessing arm is asking.
TRANSFORM_RECIPES = [
    {"name": "crop10", "note": "centre crop to 90% of each side"},
    {"name": "resize_half", "note": "downscale to 50% then back up to the original size"},
    {"name": "rejpeg_q60", "note": "re-encode as JPEG quality 60, same dimensions"},
    {"name": "watermark_band", "note": "opaque text band over the left and right thirds"},
    {"name": "letterbox", "note": "pad to square with black bars, aspect ratio preserved"},
    {"name": "crop10_rejpeg_q60", "note": "composed: crop10 then rejpeg_q60"},
]


# ---------------------------------------------------------------------------
# SQL — every statement is a SELECT. Nothing here writes.
# ---------------------------------------------------------------------------

# reltuples is the planner's estimate, read from the catalog: no scan of an 11M-row
# table just to size a TABLESAMPLE percentage.
_IMAGE_ROWCOUNT_SQL = """
    SELECT greatest(reltuples, 0)::bigint
    FROM pg_class WHERE oid = 'public.images'::regclass
"""

# P1a's scan pool. TABLESAMPLE SYSTEM is block-granular, so it scatters across the
# whole id range for the price of reading a fraction of the blocks — the same reason
# toolkit/tag_candidates.py uses it. Three narrow columns only; storage_path and
# render_score are fetched later for the handful of images that end up in a pair.
_P1A_POOL_SQL = """
    SELECT i.id, i.listing_id, i.phash
    FROM images i TABLESAMPLE SYSTEM (%(pct)s)
    WHERE i.phash IS NOT NULL
      AND i.storage_path IS NOT NULL
      AND i.listing_id IS NOT NULL
    LIMIT %(limit)s
"""

# P1a, --p1a-mode full: every exact-pHash collision group that spans more than one
# listing, corpus-wide, in ONE aggregate pass. Complete where the sampled pool is a
# recall floor — and correspondingly expensive (images has no index on phash, so this
# is a sequential scan of ~11M rows). Opted into by hand, never the default.
_P1A_COLLISIONS_SQL = """
    SELECT i.phash,
           (array_agg(i.id ORDER BY i.id))[1:%(per_group)s::int],
           (array_agg(i.listing_id ORDER BY i.id))[1:%(per_group)s::int]
    FROM images i
    WHERE i.phash IS NOT NULL
      AND i.storage_path IS NOT NULL
      AND i.listing_id IS NOT NULL
    GROUP BY i.phash
    HAVING count(*) > 1 AND min(i.listing_id) <> max(i.listing_id)
    LIMIT %(groups)s
"""

# P1b's sample: real photos, not renders and not documents. render_score NULL means
# "CLIP declined to score it" (drawings/documents), which is why the filter is on the
# tag rather than on render_score alone.
_P1B_SAMPLE_SQL = """
    SELECT i.id, i.listing_id, i.phash, i.storage_path, t.render_score, t.logical_tag
    FROM images i TABLESAMPLE SYSTEM (%(pct)s)
    JOIN image_clip_tags t ON t.image_id = i.id
    WHERE i.storage_path IS NOT NULL
      AND i.listing_id IS NOT NULL
      AND t.logical_tag <> ALL(%(document_tags)s::text[])
      AND (t.render_score IS NULL OR t.render_score < %(render_max)s)
    LIMIT %(limit)s
"""

# The ONE read of image_tag_labels, serving P2, P3 and P4 (P4 passes the document tag
# ids). Positives only — a 'negative' cell says the image is not that tag, which
# defines no population. The sealed exam is excluded through the shared anti-join.
_LABELLED_IMAGES_SQL = f"""
    SELECT l.image_id, l.tag_id, i.listing_id, li.obec_id
    FROM image_tag_labels l
    JOIN images i ON i.id = l.image_id
    JOIN listings li ON li.id = i.listing_id
    WHERE l.state = 'positive'
      AND l.source = ANY(%(sources)s::text[])
      AND l.tag_id = ANY(%(tag_ids)s::bigint[])
      AND i.storage_path IS NOT NULL
      {tag_holdout.exclusion_for("l")}
    ORDER BY l.image_id
    LIMIT %(limit)s
"""

# Active tags, and the document subset by ILIKE pattern. tag_taxonomy carries no
# label text this script may assume, so the patterns are reported back, matched or not.
_ACTIVE_TAGS_SQL = "SELECT id, label FROM tag_taxonomy WHERE active ORDER BY id"

_DOCUMENT_TAGS_SQL = """
    SELECT id, label FROM tag_taxonomy
    WHERE active AND label ILIKE ANY(%(patterns)s::text[])
    ORDER BY id
"""

# The canary fixture: deterministic by construction (lowest ids with stored bytes),
# so "a fixed handful, stable across runs" needs no hardcoded id list to rot.
_CANARY_SQL = """
    SELECT i.id, i.storage_path
    FROM images i
    WHERE i.storage_path IS NOT NULL AND i.phash IS NOT NULL
    ORDER BY i.id
    LIMIT %(limit)s
"""

_IMAGE_META_SQL = """
    SELECT i.id, i.storage_path, i.phash, i.listing_id, li.obec_id, t.render_score,
           t.logical_tag
    FROM images i
    LEFT JOIN listings li ON li.id = i.listing_id
    LEFT JOIN image_clip_tags t ON t.image_id = i.id
    WHERE i.id = ANY(%(ids)s::bigint[])
"""

# The incumbent's arm, at zero GPU cost: pgvector cosine distance over the stored
# vectors, on exactly the pairs the manifest ships. Read-only.
_PAIR_CLIP_COS_SQL = """
    SELECT p.a, p.b, (1 - (ea.embedding <=> eb.embedding))::float8
    FROM unnest(%(a)s::bigint[], %(b)s::bigint[]) AS p(a, b)
    JOIN image_clip_embeddings ea ON ea.image_id = p.a AND ea.model = %(model)s
    JOIN image_clip_embeddings eb ON eb.image_id = p.b AND eb.model = %(model)s
"""

# Six facts identify a vector; two of them (model, revision) are recorded per row for
# the incumbent since migration 456. NULL revision means "written before the pin" —
# an honest manifest says how much of the baseline is in which provenance state.
_CLIP_PROVENANCE_SQL = """
    SELECT e.model, e.revision, count(*)::bigint
    FROM image_clip_embeddings e
    WHERE e.image_id = ANY(%(ids)s::bigint[])
    GROUP BY 1, 2 ORDER BY 3 DESC
"""


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a database)
# ---------------------------------------------------------------------------

def hamming64(a: int, b: int) -> int:
    """Hamming distance between two 64-bit pHashes stored as SIGNED bigints.

    Lifted verbatim from the deleted `scripts/embedding_gpu_bench.py` (74bf82b2);
    the pod-side harness carries its own copy because it must import nothing."""
    return ((a & 0xFFFFFFFFFFFFFFFF) ^ (b & 0xFFFFFFFFFFFFFFFF)).bit_count()


def _chunks(value: int) -> tuple[int, int, int]:
    """Three disjoint slices of a 64-bit hash (22/21/21 bits)."""
    v = value & 0xFFFFFFFFFFFFFFFF
    return (v & 0x3FFFFF, (v >> 22) & 0x1FFFFF, (v >> 43) & 0x1FFFFF)


def near_duplicate_pairs(
    rows: Sequence[tuple[int, int, int]],
    *,
    max_hamming: int,
    want: int,
    bucket_max: int = 200,
) -> list[tuple[int, int, int]]:
    """P1a: (image_a, image_b, hamming) for images of DIFFERENT listings within
    `max_hamming`. rows are (image_id, listing_id, phash).

    Pigeonhole: split the 64 bits into max_hamming+1 chunks and any pair within
    max_hamming must agree exactly on at least one chunk — so bucketing by each chunk
    finds every qualifying pair without the O(n^2) compare a self-join would need.
    Three chunks cover distances up to 2; a larger --p1a-hamming falls back to the
    same three buckets and is then a RECALL floor, not a guarantee, which is what
    `partial_above_2` in the result reports.
    """
    if max_hamming < 0 or want <= 0:
        return []
    buckets: list[dict[int, list[int]]] = [defaultdict(list) for _ in range(3)]
    for idx, (_iid, _lid, phash) in enumerate(rows):
        for slot, key in enumerate(_chunks(phash)):
            buckets[slot][key].append(idx)

    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int, int]] = []
    for bucket in buckets:
        for members in bucket.values():
            if len(members) < 2 or len(members) > bucket_max:
                continue
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    ia, ib = members[i], members[j]
                    if rows[ia][1] == rows[ib][1]:
                        continue  # same listing is P2's business, not P1a's
                    a_id, b_id = rows[ia][0], rows[ib][0]
                    key = (a_id, b_id) if a_id < b_id else (b_id, a_id)
                    if key in seen:
                        continue
                    d = hamming64(rows[ia][2], rows[ib][2])
                    if d > max_hamming:
                        continue
                    seen.add(key)
                    out.append((key[0], key[1], d))
                    if len(out) >= want:
                        return out
    return out


def pairs_within_groups(
    groups: dict[Any, list[dict[str, Any]]],
    *,
    want: int,
    rng: random.Random,
    same: bool,
    key: str,
) -> list[tuple[int, int, Any]]:
    """Round-robin pairs across groups so one crowded group cannot own the sample.

    `same=True` keeps pairs whose `key` field MATCHES (P2: same listing); `same=False`
    keeps pairs whose `key` field DIFFERS (P3: different obec, P4: different listing).
    Returns (image_a, image_b, group_key).

    Bucketed by `key` rather than scanned pairwise: a tag whose 20,000 positives all
    sit in ONE obec yields no P3 pair at all, and finding that out by scanning every
    ordered pair is 4e8 comparisons for an empty answer. Bucketing makes the same
    answer O(n), and both members are consumed per pair so a group of 50 yields 25
    pairs rather than 50 pairs all sharing one photogenic image.
    """
    order = sorted(groups)
    buckets: dict[Any, dict[Any, list[dict[str, Any]]]] = {}
    for g in order:
        by_key: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for member in groups[g]:
            by_key[member[key]].append(member)
        for members in by_key.values():
            rng.shuffle(members)
        buckets[g] = dict(by_key)

    def _take(g: Any) -> tuple[dict, dict] | None:
        by_key = buckets[g]
        live = [k for k, v in by_key.items() if v]
        if same:
            for k in live:
                if len(by_key[k]) >= 2:
                    return by_key[k].pop(), by_key[k].pop()
            return None
        if len(live) < 2:
            return None
        live.sort(key=lambda k: len(by_key[k]), reverse=True)
        return by_key[live[0]].pop(), by_key[live[1]].pop()

    out: list[tuple[int, int, Any]] = []
    exhausted: set[Any] = set()
    while len(out) < want and len(exhausted) < len(order):
        for g in order:
            if g in exhausted or len(out) >= want:
                continue
            taken = _take(g)
            if taken is None:
                exhausted.add(g)
                continue
            ia, ib = int(taken[0]["image_id"]), int(taken[1]["image_id"])
            out.append((min(ia, ib), max(ia, ib), g))
    return out


def truncate_to_image_budget(
    populations: dict[str, list[tuple]],
    solo_images: Iterable[int],
    *,
    max_images: int,
) -> dict[str, list[tuple]]:
    """Round-robin across populations until the distinct-image budget is spent.

    Truncating population by population would spend the whole budget on whichever
    one happens to be listed first; the bake-off needs all five.
    """
    used: set[int] = set(int(i) for i in solo_images)
    if len(used) >= max_images:
        return {name: [] for name in populations}
    kept: dict[str, list[tuple]] = {name: [] for name in populations}
    queues = {name: list(rows) for name, rows in populations.items()}
    order = sorted(queues)
    while any(queues[name] for name in order):
        progressed = False
        for name in order:
            if not queues[name]:
                continue
            row = queues[name].pop(0)
            a, b = int(row[0]), int(row[1])
            growth = len({a, b} - used)
            if len(used) + growth > max_images:
                queues[name] = []
                continue
            used.update((a, b))
            kept[name].append(row)
            progressed = True
        if not progressed:
            break
    return kept


def _read_clip_model() -> str:
    try:
        with open(_TAXONOMY_PATH, encoding="utf-8") as fh:
            return str(json.load(fh)["model"])
    except (OSError, KeyError, ValueError):
        return _FALLBACK_CLIP_MODEL


def sample_pct(target_rows: int, total_rows: int, *, oversample: float = 2.5) -> float:
    """TABLESAMPLE percentage for a target row count.

    SYSTEM sampling is block-granular and therefore lumpy — asking for exactly enough
    blocks lands short about half the time — so the ask is oversampled and then LIMITed.
    Same reasoning as toolkit/tag_candidates.SAMPLE_OVERSAMPLE.
    """
    if total_rows <= 0:
        return 100.0
    pct = 100.0 * oversample * target_rows / total_rows
    return max(0.01, min(100.0, pct))


# ---------------------------------------------------------------------------
# Database reads
# ---------------------------------------------------------------------------

def _fetch(conn: Any, sql: str, params: dict[str, Any] | None = None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        return list(cur.fetchall())


def draw_populations(conn: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Every population, drawn read-only. Returns the raw draw; presigning happens
    afterwards so --dry-run can report sizes without minting a single URL."""
    rng = random.Random(args.seed)

    row = _fetch(conn, _IMAGE_ROWCOUNT_SQL)
    total_images = int(row[0][0]) if row else 0
    LOG.info("images (planner estimate) = %d", total_images)

    doc_rows = _fetch(conn, _DOCUMENT_TAGS_SQL, {"patterns": args.document_tag_patterns})
    doc_tag_ids = [int(r[0]) for r in doc_rows]
    LOG.info("document tags matched: %s", [r[1] for r in doc_rows] or "NONE")

    all_rows = _fetch(conn, _ACTIVE_TAGS_SQL)
    all_tag_ids = [int(r[0]) for r in all_rows]
    photo_tag_ids = [t for t in all_tag_ids if t not in set(doc_tag_ids)]

    # --- P1a: pHash-caught reposts (the control) ---------------------------
    if args.p1a_mode == "full":
        LOG.warning("P1a full mode: one sequential aggregate over images (no phash "
                    "index). Complete, but not cheap — this is the opted-into path.")
        pool = []
        for _phash, ids, listing_ids in _fetch(conn, _P1A_COLLISIONS_SQL, {
                "groups": args.p1a_pairs * 4, "per_group": 4}):
            for iid, lid in zip(ids, listing_ids):
                pool.append((int(iid), int(lid), int(_phash)))
    else:
        pool_pct = sample_pct(args.p1a_scan_images, total_images)
        pool = [(int(a), int(b), int(c))
                for a, b, c in _fetch(conn, _P1A_POOL_SQL,
                                      {"pct": pool_pct, "limit": args.p1a_scan_images})]
    p1a = near_duplicate_pairs(pool, max_hamming=args.p1a_hamming,
                               want=args.p1a_pairs, bucket_max=args.p1a_bucket_max)
    LOG.info("P1a[%s]: pool=%d pairs=%d", args.p1a_mode, len(pool), len(p1a))

    # --- P1b: the pHash-breaking synthetic population ----------------------
    p1b_pct = sample_pct(args.p1b_images, total_images)
    p1b_rows = _fetch(conn, _P1B_SAMPLE_SQL, {
        "pct": p1b_pct, "limit": args.p1b_images,
        "document_tags": CLIP_DOCUMENT_TAGS,
        "render_max": args.render_max,
    })
    p1b_images = sorted({int(r[0]) for r in p1b_rows})
    LOG.info("P1b: images=%d (pct=%.3f) x %d transforms", len(p1b_images), p1b_pct,
             len(TRANSFORM_RECIPES))

    # --- P2 / P3 / P4: label-defined populations ---------------------------
    labelled = _fetch(conn, _LABELLED_IMAGES_SQL, {
        "sources": args.label_sources, "tag_ids": photo_tag_ids,
        "limit": args.label_pool,
    })
    by_tag: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_listing_tag: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for image_id, tag_id, listing_id, obec_id in labelled:
        rec = {"image_id": int(image_id), "listing_id": int(listing_id),
               "obec_id": int(obec_id) if obec_id is not None else None}
        by_tag[int(tag_id)].append(rec)
        by_listing_tag[(int(listing_id), int(tag_id))].append(rec)
    LOG.info("labelled positives=%d over %d tags", len(labelled), len(by_tag))

    p2 = pairs_within_groups(by_listing_tag, want=args.p2_pairs, rng=rng,
                             same=True, key="listing_id")
    # P3 needs a resolved obec on both sides; a NULL there means "we do not know the
    # admin area", which cannot support "different obec".
    by_tag_geo = {t: [r for r in rows if r["obec_id"] is not None]
                  for t, rows in by_tag.items()}
    p3 = pairs_within_groups(by_tag_geo, want=args.p3_pairs, rng=rng,
                             same=False, key="obec_id")
    LOG.info("P2 pairs=%d  P3 pairs=%d", len(p2), len(p3))

    p4: list[tuple[int, int, Any]] = []
    if doc_tag_ids:
        doc_rows_labelled = _fetch(conn, _LABELLED_IMAGES_SQL, {
            "sources": args.label_sources, "tag_ids": doc_tag_ids,
            "limit": args.label_pool,
        })
        doc_by_tag: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for image_id, tag_id, listing_id, obec_id in doc_rows_labelled:
            doc_by_tag[int(tag_id)].append({
                "image_id": int(image_id), "listing_id": int(listing_id),
                "obec_id": int(obec_id) if obec_id is not None else None})
        p4 = pairs_within_groups(doc_by_tag, want=args.p4_pairs, rng=rng,
                                 same=False, key="listing_id")
    LOG.info("P4 pairs=%d (inspection only, never a scored positive set)", len(p4))

    canary = [int(r[0]) for r in _fetch(conn, _CANARY_SQL, {"limit": args.canary_images})]

    kept = truncate_to_image_budget(
        {"P1a": p1a, "P2": p2, "P3": p3, "P4": p4},
        solo_images=list(p1b_images) + canary,
        max_images=args.max_images,
    )
    return {
        "total_images_estimate": total_images,
        "document_tags": [{"id": int(r[0]), "label": r[1]} for r in doc_rows],
        "document_tag_patterns": args.document_tag_patterns,
        "P1a": kept["P1a"], "P1b": p1b_images, "P2": kept["P2"],
        "P3": kept["P3"], "P4": kept["P4"], "canary": canary,
    }


def _pair_clip_cosines(conn: Any, pairs: Sequence[tuple[int, int]],
                       model: str, batch: int = 2000) -> dict[tuple[int, int], float]:
    out: dict[tuple[int, int], float] = {}
    for start in range(0, len(pairs), batch):
        chunk = pairs[start:start + batch]
        rows = _fetch(conn, _PAIR_CLIP_COS_SQL, {
            "a": [int(a) for a, _ in chunk], "b": [int(b) for _, b in chunk],
            "model": model,
        })
        for a, b, cos in rows:
            out[(int(a), int(b))] = float(cos)
    return out


def build_manifest(conn: Any, args: argparse.Namespace) -> dict[str, Any]:
    draw = draw_populations(conn, args)
    clip_model = args.clip_model or _read_clip_model()

    pair_lists = {name: [(int(r[0]), int(r[1])) for r in draw[name]]
                  for name in ("P1a", "P2", "P3", "P4")}
    all_pairs = [p for rows in pair_lists.values() for p in rows]
    cosines = _pair_clip_cosines(conn, all_pairs, clip_model) if all_pairs else {}

    used_ids = sorted({i for p in all_pairs for i in p}
                      | set(draw["P1b"]) | set(draw["canary"]))
    meta: dict[int, dict[str, Any]] = {}
    for start in range(0, len(used_ids), 5000):
        for row in _fetch(conn, _IMAGE_META_SQL, {"ids": used_ids[start:start + 5000]}):
            iid, storage_path, phash, listing_id, obec_id, render_score, logical_tag = row
            meta[int(iid)] = {
                "key": storage_path,
                "phash": int(phash) if phash is not None else None,
                "render_score": float(render_score) if render_score is not None else None,
                "listing_id": int(listing_id) if listing_id is not None else None,
                "obec_id": int(obec_id) if obec_id is not None else None,
                "clip_tag": logical_tag,
            }

    provenance = [{"model": m, "revision": rev, "rows": int(n)}
                  for m, rev, n in _fetch(conn, _CLIP_PROVENANCE_SQL, {"ids": used_ids})]

    images: dict[str, dict[str, Any]] = {}
    if args.dry_run:
        LOG.info("dry-run: %d distinct images drawn; no URL minted, nothing uploaded",
                 len(used_ids))
    else:
        from scraper import image_storage

        if not image_storage.is_configured():
            raise RuntimeError("R2 not configured (need R2_* env vars) — cannot presign")
        r2 = image_storage.R2Client.from_env()
        for iid in used_ids:
            m = meta.get(iid)
            if not m or not m["key"]:
                continue
            images[str(iid)] = {
                "url": r2.presigned_get(m["key"], expires_in=args.expires),
                "phash": m["phash"], "render_score": m["render_score"],
                "listing_id": m["listing_id"], "obec_id": m["obec_id"],
                "clip_tag": m["clip_tag"],
            }

    def _pairs(rows: list[tuple], extra: str) -> list[dict[str, Any]]:
        out = []
        for row in rows:
            a, b = int(row[0]), int(row[1])
            out.append({"a": a, "b": b, extra: row[2],
                        "clip_cos": cosines.get((a, b), cosines.get((b, a)))})
        return out

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema": "dinov3-bakeoff-manifest/1",
        "dry_run": bool(args.dry_run),
        "url_expires_s": args.expires,
        "seed": args.seed,
        "stored_clip": {"model": clip_model, "provenance": provenance,
                        "note": "revision NULL = written before the migration-456 pin"},
        "transform_recipes": TRANSFORM_RECIPES,
        "populations": {
            "P1a": {"role": "control — reposts pHash already catches",
                    "max_hamming": args.p1a_hamming,
                    "partial_above_2": args.p1a_hamming > 2,
                    "pairs": _pairs(draw["P1a"], "hamming")},
            "P1b": {"role": "HEADLINE — same photo, pHash misses it (synthetic)",
                    "images": draw["P1b"], "transforms": [t["name"] for t in TRANSFORM_RECIPES]},
            "P2": {"role": "hard negatives — same listing, different photo",
                   "pairs": _pairs(draw["P2"], "group")},
            "P3": {"role": "HEADLINE negatives — same tag, different obec",
                   "pairs": _pairs(draw["P3"], "tag_id")},
            "P4": {"role": "INSPECTION ONLY — document tags, never a scored positive set",
                   "tags": draw["document_tags"],
                   "patterns": draw["document_tag_patterns"],
                   "pairs": _pairs(draw["P4"], "tag_id")},
        },
        "canary": {
            "image_ids": draw["canary"],
            "note": ("gated facebook/dinov3-vitb16-pretrain-lvd1689m vs the ungated "
                     "timm/vit_base_patch16_dinov3.lvd1689m mirror. timm's config "
                     "declares global_pool=avg, so the harness compares CLS-to-CLS or "
                     "reports the mismatch — it never asserts a false equality."),
        },
        "images": images,
        "n_images": len(used_ids),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="dinov3_manifest.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report population sizes; mint no URL, upload nothing.")
    ap.add_argument("--seed", type=int, default=20260905, help="Pair-draw seed.")
    ap.add_argument("--max-images", type=int, default=20000,
                    help="Distinct-image budget across every population (§5.3's ~20k).")
    ap.add_argument("--expires", type=int, default=604800,
                    help="Presigned URL validity in seconds (default 7 days).")
    ap.add_argument("--clip-model", default=None,
                    help="Stored-CLIP model name (default: data/clip_taxonomy.json).")
    ap.add_argument("--p1a-pairs", type=int, default=3000)
    ap.add_argument("--p1a-hamming", type=int, default=2,
                    help="P1a's DEFINITION. Independent of the pod harness's "
                         "--exclusion-hamming, which is the contamination filter.")
    ap.add_argument("--p1a-mode", choices=("sample", "full"), default="sample",
                    help="sample: match within a block-sampled pool (cheap, and a "
                         "RECALL FLOOR — TABLESAMPLE reads whole pages, which are "
                         "insert-time clusters, so a cross-portal repost whose two "
                         "copies landed months apart can fall outside the pool). "
                         "full: one corpus-wide aggregate over exact-pHash collision "
                         "groups (complete, one sequential scan of images).")
    ap.add_argument("--p1a-scan-images", type=int, default=1_000_000,
                    help="Block-sampled pool P1a pairs are matched within (sample "
                         "mode). The one knob that costs real DB time AND runner "
                         "memory — the pool and its three chunk indexes are held in "
                         "RAM — so raise it deliberately when the yield is thin.")
    ap.add_argument("--p1a-bucket-max", type=int, default=200,
                    help="Skip chunk buckets larger than this (blank images collide).")
    ap.add_argument("--p1b-images", type=int, default=3000)
    ap.add_argument("--p2-pairs", type=int, default=3000)
    ap.add_argument("--p3-pairs", type=int, default=4000)
    ap.add_argument("--p4-pairs", type=int, default=1000)
    ap.add_argument("--canary-images", type=int, default=8)
    ap.add_argument("--label-pool", type=int, default=200_000,
                    help="Cap on positive label rows read per population family.")
    ap.add_argument("--label-sources", default="human,human_confirmed,machine",
                    help="image_tag_labels.source values that define a population.")
    ap.add_argument("--render-max", type=float, default=0.95,
                    help="P1b skips images whose render_score is at/above this.")
    ap.add_argument("--document-tags", default=",".join(DEFAULT_DOCUMENT_TAG_PATTERNS),
                    help="ILIKE patterns selecting P4's document tags.")
    ap.add_argument("--r2-key", default=None,
                    help="Upload the manifest to this R2 key as well as writing it locally.")
    args = ap.parse_args(argv)
    args.label_sources = [s.strip() for s in args.label_sources.split(",") if s.strip()]
    args.document_tag_patterns = [p.strip() for p in args.document_tags.split(",") if p.strip()]
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL not set", file=sys.stderr)
        return 2

    import psycopg

    with psycopg.connect(os.environ["SUPABASE_DB_URL"], autocommit=True,
                         prepare_threshold=None) as conn:
        manifest = build_manifest(conn, args)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    LOG.info("wrote %s (%.2f MB, %d images)", args.out,
             os.path.getsize(args.out) / 1e6, manifest["n_images"])

    if args.r2_key and not args.dry_run:
        from scraper import image_storage

        image_storage.R2Client.from_env().upload_file(
            args.r2_key, args.out, content_type="application/json")
        LOG.info("uploaded manifest to r2://%s", args.r2_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
