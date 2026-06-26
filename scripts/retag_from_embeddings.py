"""Re-run the CLIP zero-shot tagging over ALREADY-tagged images, from their STORED
embeddings (image_clip_embeddings) — NO R2 download, NO image re-inference, just the
taxonomy's text anchors dotted with each stored vector. The way to apply a TAXONOMY change
(new logical tags, sharpened anchors) to the back catalogue cheaply.

Campaign-driven + resumable: re-tags every row whose `image_clip_tags.tagged_at` is older
than `app_settings.clip_taxonomy_retag_after` (the campaign cutoff — set it to now() to start
a re-tag after a taxonomy edit), and stamps `tagged_at = now()` on each, so a re-tagged row
drops out of the scan. No cutoff set → nothing to do. Sharded (--shard k/--shards N) like
clip_tag. Active-listing images only (that is where embeddings are stored). New / not-yet-
tagged images go through the normal clip_tag backfill, which loads the same live taxonomy.
Install: CPU torch + `.[clip]`.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

LOG = logging.getLogger("retag_from_embeddings")

_CUTOFF_SQL = "SELECT value #>> '{}' FROM app_settings WHERE key = 'clip_taxonomy_retag_after'"

_SELECT = """
    SELECT t.image_id, t.model, e.embedding
    FROM image_clip_tags t
    JOIN image_clip_embeddings e ON e.image_id = t.image_id AND e.model = t.model
    WHERE t.tagged_at < %(cutoff)s {shard}
    LIMIT %(limit)s
"""
_UPDATE = (
    "UPDATE image_clip_tags SET fine_tag = %s, logical_tag = %s, confidence = %s, "
    "render_score = %s, tagged_at = now() WHERE image_id = %s AND model = %s"
)


def _parse_vec(v: Any) -> list[float]:
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

    with psycopg.connect(os.environ["SUPABASE_DB_URL"], autocommit=True,
                         prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(_CUTOFF_SQL)
            row = cur.fetchone()
        cutoff = row[0] if row else None
        if not cutoff:
            LOG.info("RETAG no campaign: app_settings.clip_taxonomy_retag_after is unset")
            return 0
        LOG.info("RETAG campaign cutoff=%s shard=%d/%d", cutoff, args.shard, args.shards)

        shard_clause = ""
        extra: dict[str, Any] = {"cutoff": cutoff}
        if args.shards > 1:
            shard_clause = "AND (t.image_id %% %(shards)s) = %(shard)s"
            extra.update({"shards": args.shards, "shard": args.shard})
        sql = _SELECT.format(shard=shard_clause)

        # Pre-check BEFORE loading the CLIP model: once the campaign has drained, a scheduled
        # run is a cheap no-op instead of a ~30s model load that finds nothing.
        with conn.cursor() as cur:
            cur.execute(sql, {"limit": 1, **extra})
            if not cur.fetchone():
                LOG.info("RETAG nothing pending for this shard; campaign drained")
                return 0

        tagger = Tagger.load()

        deadline = time.monotonic() + args.max_seconds
        written = 0
        while time.monotonic() < deadline:
            if args.limit and written >= args.limit:
                break
            batch = args.batch_size if not args.limit else min(args.batch_size, args.limit - written)
            with conn.cursor() as cur:
                cur.execute(sql, {"limit": batch, **extra})
                rows = cur.fetchall()
            if not rows:
                break
            embs = np.array([_parse_vec(r[2]) for r in rows], dtype=np.float32)
            results = tagger.tags_from_emb(torch.from_numpy(embs))
            params = [
                (tr.fine_tag, tr.logical_tag, tr.confidence, tr.render_score, r[0], r[1])
                for r, tr in zip(rows, results)
            ]
            with conn.cursor() as cur:
                cur.executemany(_UPDATE, params)
            written += len(rows)
            LOG.info("RETAG written=%d batch=%d", written, len(rows))

    LOG.info("RETAG done written=%d", written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
