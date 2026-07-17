"""Build the GPU embedding-bench manifest — labelled pairs + presigned image URLs.

Runs where the secrets live (embedding_ab.yml `manifest` mode in GitHub Actions); the
GPU pod that consumes it needs NO credentials: every image ships as a presigned R2 GET
URL (7-day expiry), labels come from `dedup_label_events` (migration 300), and the
stored CLIP cosine rides along per image-pair so scripts/embedding_gpu_bench.py can
compare encoders on byte-identical candidate pairs. READ-ONLY (SELECT + presign only).

Candidate image pairs are same-family only (image_clip_tags logical tags), capped at
--cap images per (listing, family) so a photo-heavy listing cannot blow up the cross
product, then round-robin truncated to --pair-cap image pairs per labelled pair.
Render filtering is NOT applied here — render_score ships raw so the bench can toggle
exclusions offline.

Run: `python -m scripts.build_embedding_manifest --out embedding_manifest.json`
(env: SUPABASE_DB_URL + R2_*).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

LOG = logging.getLogger("embedding_manifest")

DEFAULT_FAMILIES = [
    "kitchen", "bathroom", "toilet", "living_room", "bedroom", "hallway",
    "exterior_facade", "garden", "balcony_terrace",
    # 'other' carries most pozemek (raw land) imagery — without it the site-plan
    # hard-negative stratum loses half its pairs (v1 manifest: 39/80 negatives).
    "other",
]

# One row per candidate same-family image pair of a labelled listing pair. The
# embeddings join is model-scoped through the tag row (ea.model = a.model), matching
# scripts/embedding_ab.py; clip_cos is NULL when either side has no stored vector.
_MANIFEST_SQL = """
WITH pairs AS (
    SELECT label_id, left_listing_id AS la, right_listing_id AS lb,
           is_same, coalesce(category_main, 'unknown') AS category_main, label_source
    FROM dedup_label_events
    WHERE left_listing_id IS NOT NULL AND right_listing_id IS NOT NULL
),
sides AS (
    SELECT la AS sreality_id FROM pairs UNION SELECT lb FROM pairs
),
imgs AS (
    SELECT i.id, i.sreality_id, i.storage_path, i.phash,
           t.logical_tag, t.render_score, t.model,
           row_number() OVER (PARTITION BY i.sreality_id, t.logical_tag ORDER BY i.id) AS rn
    FROM images i
    JOIN image_clip_tags t ON t.image_id = i.id
    JOIN sides s ON s.sreality_id = i.sreality_id
    WHERE i.storage_path IS NOT NULL
      AND t.logical_tag = ANY(%(families)s)
)
SELECT p.label_id, p.la, p.lb, p.is_same, p.category_main, p.label_source,
       a.logical_tag,
       a.id AS ia_id, a.storage_path AS ia_key, a.phash AS ia_phash, a.render_score AS ia_render,
       b.id AS ib_id, b.storage_path AS ib_key, b.phash AS ib_phash, b.render_score AS ib_render,
       (1 - (ea.embedding <=> eb.embedding)) AS clip_cos
FROM pairs p
JOIN imgs a ON a.sreality_id = p.la AND a.rn <= %(cap)s
JOIN imgs b ON b.sreality_id = p.lb AND b.logical_tag = a.logical_tag AND b.rn <= %(cap)s
LEFT JOIN image_clip_embeddings ea ON ea.image_id = a.id AND ea.model = a.model
LEFT JOIN image_clip_embeddings eb ON eb.image_id = b.id AND eb.model = b.model
"""


def _round_robin_truncate(rows: list[tuple], pair_cap: int) -> list[tuple]:
    """Keep at most pair_cap image pairs, drawing evenly across families so one
    photo-heavy family cannot crowd out the others. rows: (tag, image_pair_dict)."""
    if len(rows) <= pair_cap:
        return rows
    by_tag: dict[str, list[tuple]] = defaultdict(list)
    for r in rows:
        by_tag[r[0]].append(r)
    out: list[tuple] = []
    queues = [by_tag[t] for t in sorted(by_tag)]
    i = 0
    while len(out) < pair_cap and any(queues):
        q = queues[i % len(queues)]
        if q:
            out.append(q.pop(0))
        i += 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="embedding_manifest.json")
    ap.add_argument("--families", default=",".join(DEFAULT_FAMILIES))
    ap.add_argument("--cap", type=int, default=6,
                    help="Max images per (listing, family) entering the cross product.")
    ap.add_argument("--pair-cap", type=int, default=200,
                    help="Max candidate image pairs kept per labelled pair.")
    ap.add_argument("--max-pairs", type=int, default=0,
                    help="Debug: keep only the first N labelled pairs (0 = all).")
    ap.add_argument("--expires", type=int, default=604800,
                    help="Presigned URL validity in seconds (default 7 days).")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL not set", file=sys.stderr)
        return 2

    import psycopg

    from scraper import image_storage

    if not image_storage.is_configured():
        print("ERROR: R2 not configured (need R2_* env vars)", file=sys.stderr)
        return 2

    families = [f.strip() for f in args.families.split(",") if f.strip()]
    with psycopg.connect(os.environ["SUPABASE_DB_URL"], autocommit=True,
                         prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(_MANIFEST_SQL, {"families": families, "cap": args.cap})
            rows = cur.fetchall()
    LOG.info("candidate image-pair rows=%d", len(rows))

    images: dict[int, dict] = {}
    pair_meta: dict[str, dict] = {}
    pair_rows: dict[str, list[tuple]] = defaultdict(list)
    for (label_id, la, lb, is_same, category, source, tag,
         ia_id, ia_key, ia_phash, ia_render,
         ib_id, ib_key, ib_phash, ib_render, clip_cos) in rows:
        pair_meta.setdefault(label_id, {
            "pair_id": label_id, "left": int(la), "right": int(lb),
            "is_same": bool(is_same), "category": category, "source": source,
        })
        for iid, key, phash, render in ((ia_id, ia_key, ia_phash, ia_render),
                                        (ib_id, ib_key, ib_phash, ib_render)):
            images.setdefault(int(iid), {
                "key": key,
                "phash": int(phash) if phash is not None else None,
                "render_score": float(render) if render is not None else None,
            })
        pair_rows[label_id].append((tag, {
            "a": int(ia_id), "b": int(ib_id), "tag": tag,
            "clip_cos": float(clip_cos) if clip_cos is not None else None,
        }))

    pair_ids = sorted(pair_rows)
    if args.max_pairs:
        pair_ids = pair_ids[: args.max_pairs]

    pairs = []
    used_images: set[int] = set()
    for pid in pair_ids:
        kept = _round_robin_truncate(pair_rows[pid], args.pair_cap)
        image_pairs = [r[1] for r in kept]
        for ip in image_pairs:
            used_images.add(ip["a"])
            used_images.add(ip["b"])
        pairs.append({**pair_meta[pid], "image_pairs": image_pairs})

    LOG.info("labelled pairs=%d unique images=%d", len(pairs), len(used_images))

    r2 = image_storage.R2Client.from_env()
    out_images = {}
    for iid in sorted(used_images):
        meta = images[iid]
        out_images[str(iid)] = {
            "url": r2.presigned_get(meta["key"], expires_in=args.expires),
            "phash": meta["phash"],
            "render_score": meta["render_score"],
        }

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "url_expires_s": args.expires,
        "families": families,
        "cap": args.cap,
        "pair_cap": args.pair_cap,
        "images": out_images,
        "pairs": pairs,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    LOG.info("wrote %s (%.1f MB)", args.out, os.path.getsize(args.out) / 1e6)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
