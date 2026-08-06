"""Backfill dedup_sim.label_proposals: the secondary CLIP encoder's Taxonomy v1
tag guess for images in the Labeling program's sample.

Selects images that are (a) in dedup_sim.labeling_sample and (b) not yet
proposed-for by the CURRENT secondary model (dedup_sim_settings'
labeling_secondary_model — a model-id change naturally re-proposes, since
label_proposals is keyed (image_id, model), mirroring image_clip_tags).
Downloads bytes from R2, scores against whatever labels are currently
active in dedup_sim.taxonomy_labels, upserts one proposal row per image.
No-op (exit 0) if R2 env vars are missing, or if the taxonomy is still
empty (nothing to score against yet — the operator adds labels through the
Labeling page first).

Dispatch-only (no cron): the Labeling program grows its sample in bursts as
the operator works through it, per PROGRAM.md's compute-placement
convention (GH Actions dispatch for evidence generation).

Usage:  python -m scripts.label_proposal_backfill --limit 2000 --shard 0 --shards 2
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

LOG = logging.getLogger("label_proposal_backfill")

_SELECT_PENDING = """
    SELECT s.image_id, i.storage_path
    FROM dedup_sim.labeling_sample s
    JOIN images i ON i.id = s.image_id
    WHERE i.storage_path IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM dedup_sim.label_proposals p
        WHERE p.image_id = s.image_id AND p.model = %(model)s
      )
      AND (%(shards)s = 1 OR s.image_id %% %(shards)s = %(shard)s)
    ORDER BY s.added_at DESC
    LIMIT %(limit)s
"""

_UPSERT_SQL = """
    INSERT INTO dedup_sim.label_proposals (image_id, model, label, confidence)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (image_id, model) DO NOTHING
"""


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _download_decode(r2: "image_storage.R2Client", rows: list, workers: int):
    """(image_id, RGB image) for every row whose stored bytes decode. A
    download failure (transient R2 blip) or a decode failure (corrupt /
    non-image) both just drop the row for this run — retried next run,
    since (unlike the production tagger) there's no in-table marker here
    to terminal-mark against."""
    from PIL import Image  # base dep

    def _one(row):
        image_id, key = row[0], row[1]
        try:
            data = r2.download_bytes(key)
            return image_id, Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:  # noqa: BLE001 - transient/corrupt: skip, retry next run
            return image_id, None

    decoded: list = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for image_id, img in pool.map(_one, rows):
            if img is not None:
                decoded.append((image_id, img))
    return decoded


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=2000, help="Max images per run.")
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1, help="image_id %% shards == shard.")
    p.add_argument("--workers", type=int, default=16, help="Parallel R2 downloads.")
    p.add_argument("--chunk", type=int, default=128,
                   help="Images per download+tag+commit cycle (bounds memory).")
    p.add_argument("--batch-size", type=int, default=16, help="CLIP encode batch.")
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
        LOG.info("LABEL_PROPOSAL skip: R2 env vars missing")
        return 0

    import psycopg

    from toolkit import dedup_sim_settings as dss

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        model = dss.effective_value("labeling_secondary_model", conn=conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT label FROM dedup_sim.taxonomy_labels WHERE active ORDER BY label"
            )
            labels = [r[0] for r in cur.fetchall()]
        if not labels:
            LOG.info("LABEL_PROPOSAL skip: no active taxonomy labels yet")
            return 0

        with conn.cursor() as cur:
            cur.execute(
                _SELECT_PENDING,
                {"model": model, "shards": args.shards, "shard": args.shard,
                 "limit": args.limit},
            )
            rows = cur.fetchall()
        LOG.info("LABEL_PROPOSAL pending=%d model=%s labels=%d shard=%d/%d dry_run=%s",
                 len(rows), model, len(labels), args.shard, args.shards, args.dry_run)
        if args.dry_run or not rows:
            return 0

        from scraper.label_proposal_tagger import ProposalTagger

        tagger = ProposalTagger.load(model, labels, args.threads)
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
                (image_id, model, r.label, r.confidence)
                for image_id, r in zip(ids, results)
            ]
            with conn.cursor() as cur:
                cur.executemany(_UPSERT_SQL, params)
            written += len(params)
            LOG.info("LABEL_PROPOSAL progress=%d/%d errors=%d", written, len(rows), errors)

    LOG.info("LABEL_PROPOSAL done written=%d errors=%d", written, errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
