"""Backfill image_clip_tags.render_score for already-tagged images (migration 239).

The clip_tag backfill skips already-tagged images (clip_tagged_at IS NOT NULL), so every
image tagged BEFORE the render axis shipped has render_score NULL — the listing-detail
render badge stays hidden and the byt render exclusion is inert on them. This re-scores
the render-vs-photo axis from each image's STORED CLIP embedding
(image_clip_embeddings) — NO R2 download, NO image re-inference, just the render/photo
text anchors dotted with the stored vector — and writes render_score.

Active-listing images only (that is where embeddings are stored, per the backfill);
inactive images keep NULL until/unless re-embedded. Idempotent + resumable: a scored row
drops out of the `render_score IS NULL` scan (backed by the partial index, migration 240).
Sharded (--shard k/--shards N) like clip_tag. Install: CPU torch + `.[clip]`.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

LOG = logging.getLogger("backfill_render_score")

_SELECT = """
    SELECT t.image_id, t.model, e.embedding
    FROM image_clip_tags t
    JOIN image_clip_embeddings e ON e.image_id = t.image_id AND e.model = t.model
    WHERE t.render_score IS NULL {shard}
    LIMIT %(limit)s
"""
_UPDATE = "UPDATE image_clip_tags SET render_score = %s WHERE image_id = %s AND model = %s"


def _parse_vec(v: Any) -> list[float]:
    # pgvector text form '[f,f,...]' (psycopg returns it as a string unless a parser is
    # registered); fall back to a list if one is.
    if isinstance(v, str):
        return [float(x) for x in v.strip("[]").split(",") if x]
    return list(v)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--batch-size", type=int, default=512)
    p.add_argument("--max-seconds", type=int, default=3000)
    p.add_argument("--limit", type=int, default=0, help="Total cap (0 = until time budget).")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL not set", file=sys.stderr)
        return 2

    import numpy as np
    import psycopg
    import torch

    from scraper.clip_tagger import Tagger

    tagger = Tagger.load()

    shard_clause = ""
    extra: dict[str, Any] = {}
    if args.shards > 1:
        shard_clause = "AND (t.image_id %% %(shards)s) = %(shard)s"
        extra = {"shards": args.shards, "shard": args.shard}
    sql = _SELECT.format(shard=shard_clause)

    deadline = time.monotonic() + args.max_seconds
    written = 0
    with psycopg.connect(os.environ["SUPABASE_DB_URL"], autocommit=True,
                         prepare_threshold=None) as conn:
        while time.monotonic() < deadline:
            if args.limit and written >= args.limit:
                break
            batch = args.batch_size
            if args.limit:
                batch = min(batch, args.limit - written)
            with conn.cursor() as cur:
                cur.execute(sql, {"limit": batch, **extra})
                rows = cur.fetchall()
            if not rows:
                break
            embs = np.array([_parse_vec(r[2]) for r in rows], dtype=np.float32)
            scores = tagger.render_scores_from_emb(torch.from_numpy(embs))
            with conn.cursor() as cur:
                cur.executemany(_UPDATE, [(s, r[0], r[1]) for s, r in zip(scores, rows)])
            written += len(rows)
            LOG.info("RENDER_BACKFILL written=%d batch=%d", written, len(rows))

    LOG.info("RENDER_BACKFILL done written=%d", written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
