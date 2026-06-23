"""Backfill image_clip_tags: the self-hosted CLIP tagger's room/plot tag per image.

Selects stored images without a CLIP tag (for the current model) — ACTIVE-listing
images first — downloads bytes from R2 on a worker pool, tags them with the shared
Tagger (CLIP zero-shot, collapsed to the engine's logical labels), and upserts one
row per image. Idempotent + resumable (a tagged image drops out of the next select)
and shardable (image_id % shards), so it parallelises like images.yml. A FREE
replacement for the paid room classifier on the coarse dedup-relevant tags, and the
first tagger for dum/pozemek/komercni. No-op (exit 0) if R2 env vars are missing.

Usage:  python -m scripts.clip_tag_backfill --limit 20000 --shard 0 --shards 4
Required: SUPABASE_DB_URL (+ R2_* and the `clip` extra to do the work).
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from scraper import image_storage

LOG = logging.getLogger("clip_tag_backfill")

# Pending = stored image with no tag for THIS model. ACTIVE-listing images first
# (dedup-relevant), then newest. `id %% shards = shard` partitions across runners.
_SELECT_SQL = """
    SELECT i.id, i.storage_path
    FROM images i
    LEFT JOIN listings l ON l.sreality_id = i.sreality_id
    LEFT JOIN image_clip_tags t ON t.image_id = i.id AND t.model = %(model)s
    WHERE i.storage_path IS NOT NULL
      AND t.image_id IS NULL
      AND (%(shards)s = 1 OR i.id %% %(shards)s = %(shard)s)
    ORDER BY (l.is_active IS TRUE) DESC, i.id DESC
    LIMIT %(limit)s
"""

_UPSERT_SQL = """
    INSERT INTO image_clip_tags (image_id, model, fine_tag, logical_tag, confidence)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (image_id, model) DO UPDATE
      SET fine_tag = EXCLUDED.fine_tag, logical_tag = EXCLUDED.logical_tag,
          confidence = EXCLUDED.confidence, tagged_at = now()
"""


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _download_decode(r2: image_storage.R2Client, rows: list[tuple[int, str]],
                     workers: int):
    from PIL import Image  # base dep

    def _one(row: tuple[int, str]):
        image_id, key = row
        try:
            img = Image.open(io.BytesIO(r2.download_bytes(key))).convert("RGB")
            return image_id, img
        except Exception:  # noqa: BLE001 - one bad image must not kill the run
            return image_id, None

    out = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for image_id, img in pool.map(_one, rows):
            if img is not None:
                out.append((image_id, img))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=20000, help="Max images per run.")
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1, help="image_id %% shards == shard.")
    p.add_argument("--workers", type=int, default=16, help="Parallel R2 downloads.")
    p.add_argument("--chunk", type=int, default=256,
                   help="Images per download+tag+commit cycle (bounds memory).")
    p.add_argument("--batch-size", type=int, default=32, help="CLIP encode batch.")
    p.add_argument("--threads", type=int, default=0, help="torch threads (0=cpus).")
    p.add_argument("--dry-run", action="store_true",
                   help="Report the pending count and exit without tagging.")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    if not image_storage.is_configured():
        LOG.info("CLIP_TAG skip: R2 env vars missing")
        return 0

    import psycopg

    from scraper.clip_tagger import Tagger, load_taxonomy

    model = load_taxonomy()["model"]  # the SELECT keys on it; cheap, no torch

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT_SQL, {"model": model, "limit": args.limit,
                                      "shards": args.shards, "shard": args.shard})
            rows = [(r[0], r[1]) for r in cur.fetchall()]
        LOG.info("CLIP_TAG pending=%d shard=%d/%d model=%s dry_run=%s",
                 len(rows), args.shard, args.shards, model, args.dry_run)
        if args.dry_run or not rows:
            return 0

        tagger = Tagger.load(args.threads)  # loads the model once
        r2 = image_storage.R2Client.from_env(max_pool_connections=args.workers + 4)
        written = errors = 0
        for chunk in _chunks(rows, args.chunk):
            decoded = _download_decode(r2, chunk, args.workers)
            errors += len(chunk) - len(decoded)
            if not decoded:
                continue
            ids = [d[0] for d in decoded]
            results = tagger.tag([d[1] for d in decoded], args.batch_size)
            params = [
                (image_id, model, r.fine_tag, r.logical_tag, r.confidence)
                for image_id, r in zip(ids, results)
            ]
            with conn.cursor() as cur:
                cur.executemany(_UPSERT_SQL, params)
            written += len(params)
            LOG.info("CLIP_TAG progress=%d/%d errors=%d", written, len(rows), errors)

    LOG.info("CLIP_TAG done written=%d errors=%d", written, errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
