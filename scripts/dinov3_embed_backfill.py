"""Backfill image_dinov3_embeddings: one L2-normalized 768-d DINOv3 vector per stored
image, under the six-fact encoder identity in data/dinov3_config.json.

This is the PAYLOAD a GPU pod runs (scripts/dinov3_embed_dispatch.py launches the pod);
it knows nothing about RunPod and runs identically on a plain runner. See
docs/design/new-dedup/ENCODER-DECISION.md §5.5 for the execution plan this implements.

CHECKPOINT/RESUME WITH NO NEW SCHEMA. There is no marker column and none is wanted: the
target table IS the checkpoint. Pending = a stored image with no row under this EXACT
six-fact config (an anti-join), so a pod dying at 60% costs minutes, a re-run is a
no-op, and an image embedded under a DIFFERENT config is still pending under this one —
which is the whole point of the six-fact key. Progress is therefore always answerable
in SQL: count(rows for this config) / count(images.storage_path IS NOT NULL).

STREAMS, never stages. The old bake-off harness downloaded every image to local disk
first; at 10.4M images that is structurally impossible (§5.5). Each --chunk is
downloaded, decoded, embedded, written and dropped.

WRITE-RATE THROTTLE, and why --max-write-mb-per-hour has no default: Supabase gp3 disk
AUTO-EXPANDS at 90% of allocated disk and the project goes READ-ONLY at 95% with the
quota exhausted — which takes down the scrapers, the API's writes, the SPA and the
pipeline, not just this job. Disk also cannot shrink. The safe rate therefore depends
on the dashboard's live disk-utilisation reading at run time and cannot be baked into a
default, so the flag is required and the operator must look before dispatching.

Usage:  python -m scripts.dinov3_embed_backfill --max-write-mb-per-hour 500 --limit 200000
Required: SUPABASE_DB_URL (+ R2_*, HF_TOKEN and the `clip` extra to do the work).
Requires migration 480 (PR #1296) to have been applied — the table does not exist otherwise.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from scraper import image_storage
from scraper.dinov3_config import IDENTITY_FIELDS, encoder_identity

LOG = logging.getLogger("dinov3_embed_backfill")

# A halfvec(768) row on disk: 768 x 2 B payload + the varlena/halfvec headers + the
# key columns and tuple header ~= 1,552 B. The identity index stores its own copy on
# top, so this is a FLOOR on bytes written, not a guarantee — set the ceiling with
# margin against the dashboard reading.
HALFVEC_ROW_BYTES = 1552

# The pending scan is a large anti-join during the bulk phase and the pooler's 2-min
# OLTP default is the wrong limit for it (the same reasoning as clip_tag_backfill).
SELECT_TIMEOUT_MS = 300_000

# The checkpoint. `i.id > %(after_id)s` is an IN-RUN cursor only: it stops a chunk whose
# images all failed to download (a transient R2 blip writes no rows) from being selected
# forever inside one run. A fresh run starts at 0 again, so a transient failure is
# retried on the next pass while a permanent one costs one download per run, not a wedge.
_PENDING_SQL = """
    SELECT i.id, i.storage_path
    FROM images i
    WHERE i.storage_path IS NOT NULL
      AND i.id > %(after_id)s
      AND (%(shards)s = 1 OR i.id %% %(shards)s = %(shard)s)
      AND NOT EXISTS (
        SELECT 1 FROM image_dinov3_embeddings e
        WHERE e.image_id = i.id
          AND e.model = %(model)s
          AND e.revision = %(revision)s
          AND e.library = %(library)s
          AND e.pooling = %(pooling)s
          AND e.resolution = %(resolution)s
          AND e.preprocessing = %(preprocessing)s
          AND e.dtype = %(dtype)s
      )
    ORDER BY i.id
    LIMIT %(batch)s
"""

_PENDING_COUNT_SQL = """
    SELECT count(*)
    FROM images i
    WHERE i.storage_path IS NOT NULL
      AND (%(shards)s = 1 OR i.id %% %(shards)s = %(shard)s)
      AND NOT EXISTS (
        SELECT 1 FROM image_dinov3_embeddings e
        WHERE e.image_id = i.id
          AND e.model = %(model)s
          AND e.revision = %(revision)s
          AND e.library = %(library)s
          AND e.pooling = %(pooling)s
          AND e.resolution = %(resolution)s
          AND e.preprocessing = %(preprocessing)s
          AND e.dtype = %(dtype)s
      )
"""

_EMBEDDED_COUNT_SQL = """
    SELECT count(*)
    FROM image_dinov3_embeddings e
    WHERE e.model = %(model)s
      AND e.revision = %(revision)s
      AND e.library = %(library)s
      AND e.pooling = %(pooling)s
      AND e.resolution = %(resolution)s
      AND e.preprocessing = %(preprocessing)s
      AND e.dtype = %(dtype)s
"""

_TOTAL_IMAGES_SQL = "SELECT count(*) FROM images WHERE storage_path IS NOT NULL"

# DO NOTHING, never DO UPDATE: a different config is a different ROW (that is what the
# six-fact key means), and the same config re-embedding the same image recomputes a
# byte-identical vector — so there is no meaningful update case, only a wasted write.
_INSERT_SQL = """
    INSERT INTO image_dinov3_embeddings
      (image_id, model, revision, library, pooling, resolution, preprocessing, dtype, embedding)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::halfvec)
    ON CONFLICT (image_id, model, revision, library, pooling, resolution, preprocessing, dtype)
    DO NOTHING
"""


class WriteThrottle:
    """Paces writes to a bytes/hour ceiling by sleeping between batches.

    Sleep and clock are injectable so the arithmetic is testable without waiting.
    """

    def __init__(
        self,
        mb_per_hour: float,
        *,
        sleep: Callable[[float], Any] = time.sleep,
        row_bytes: int = HALFVEC_ROW_BYTES,
    ) -> None:
        if mb_per_hour <= 0:
            raise ValueError("--max-write-mb-per-hour must be > 0")
        self.mb_per_hour = float(mb_per_hour)
        self.bytes_per_second = self.mb_per_hour * 1024 * 1024 / 3600.0
        self.row_bytes = row_bytes
        self._sleep = sleep
        self.slept_s = 0.0

    def budget_s(self, rows: int) -> float:
        """How long `rows` worth of bytes is allowed to take at the configured rate."""
        return (rows * self.row_bytes) / self.bytes_per_second

    def pace(self, rows: int, elapsed_s: float) -> float:
        """Sleep off whatever of the batch's byte budget the batch did not already
        spend in wall time. Returns the delay slept (0.0 when already slower than the
        ceiling)."""
        delay = self.budget_s(rows) - elapsed_s
        if delay <= 0:
            return 0.0
        self._sleep(delay)
        self.slept_s += delay
        return delay


def _vec_str(row) -> str:
    """A normalized embedding row -> pgvector's text form '[f,f,...]' (halfvec parses it)."""
    return "[" + ",".join(f"{x:.6f}" for x in row.tolist()) + "]"


def _download_decode(r2, rows: list, workers: int):
    """(decoded, failed): decoded = [(image_id, RGB image)]. A failure — transient R2
    error or bytes that will never decode — is simply left unwritten; the anti-join
    picks the image up again on the next run, and the in-run cursor stops it wedging
    this one. Nothing is staged on local disk."""
    from PIL import Image  # base dep

    def _one(row):
        image_id, key = row[0], row[1]
        try:
            data = r2.download_bytes(key)
        except Exception:  # noqa: BLE001 - transient R2 error: retried next run
            return image_id, None
        try:
            return image_id, Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:  # noqa: BLE001 - stored bytes won't decode
            return image_id, None

    decoded: list = []
    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for image_id, img in pool.map(_one, rows):
            if img is None:
                failed += 1
            else:
                decoded.append((image_id, img))
    return decoded, failed


def _scalar(conn, sql: str, params: dict[str, Any] | None = None) -> int:
    with conn.transaction(), conn.cursor() as cur:
        # SET is a utility statement — it cannot take a bound parameter.
        cur.execute(f"SET LOCAL statement_timeout = {int(SELECT_TIMEOUT_MS)}")
        cur.execute(sql, params or {})
        row = cur.fetchone()
    return int(row[0]) if row else 0


def select_pending(conn, *, identity: dict[str, Any], batch: int, shard: int,
                   shards: int, after_id: int) -> list[tuple[int, str]]:
    """One chunk of images with no vector under this exact six-fact identity."""
    params = {**identity, "batch": batch, "shard": shard, "shards": shards,
              "after_id": after_id}
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(f"SET LOCAL statement_timeout = {int(SELECT_TIMEOUT_MS)}")
        cur.execute(_PENDING_SQL, params)
        return [(r[0], r[1]) for r in cur.fetchall()]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--max-write-mb-per-hour", type=float, required=True,
                   help="REQUIRED, no default. Ceiling on vector bytes written per hour. "
                        "The safe value depends on the Supabase dashboard's LIVE disk-"
                        "utilisation reading at run time: gp3 disk auto-expands at 90%% of "
                        "allocated disk and the project goes READ-ONLY at 95%% with the "
                        "quota exhausted — which takes the scrapers, the API's writes, the "
                        "SPA and the pipeline down with it, not just this job. Disk cannot "
                        "shrink. Look at the dashboard, then pass a number.")
    p.add_argument("--limit", type=int, default=200_000, help="Max images per run.")
    p.add_argument("--chunk", type=int, default=256,
                   help="Images per download+embed+commit cycle (bounds memory).")
    p.add_argument("--batch-size", type=int, default=32, help="Model forward batch.")
    p.add_argument("--workers", type=int, default=16, help="Parallel R2 downloads.")
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1, help="image_id %% shards == shard.")
    p.add_argument("--threads", type=int, default=0, help="torch threads (0=cpus).")
    p.add_argument("--max-seconds", type=float, default=0,
                   help="Time budget; stop cleanly at a chunk boundary. 0 = unbounded. "
                        "Set it under the runner/pod timeout so the pass reports what it "
                        "wrote instead of being killed mid-flight.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report the resolved encoder identity and the pending count, then "
                        "exit. Downloads nothing, embeds nothing, writes nothing.")
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

    # Raises unless all six facts are set — the refuse-to-run rail. Deliberately BEFORE
    # the R2 guard: an under-specified encoder is a hard error, not a skip.
    identity = encoder_identity()
    LOG.info("DINOV3 identity %s",
             " ".join(f"{k}={identity[k]}" for k in IDENTITY_FIELDS))

    import psycopg

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        total_images = _scalar(conn, _TOTAL_IMAGES_SQL)
        embedded_before = _scalar(conn, _EMBEDDED_COUNT_SQL, identity)
        if args.dry_run:
            pending = _scalar(conn, _PENDING_COUNT_SQL,
                              {**identity, "shard": args.shard, "shards": args.shards})
            LOG.info("DINOV3 dry_run pending=%d shard=%d/%d embedded=%d/%d (%.2f%%) "
                     "max_write_mb_per_hour=%.1f",
                     pending, args.shard, args.shards, embedded_before, total_images,
                     100.0 * embedded_before / total_images if total_images else 0.0,
                     args.max_write_mb_per_hour)
            return 0

        if not image_storage.is_configured():
            LOG.info("DINOV3 skip: R2 env vars missing")
            return 0

        from scraper.dinov3_tagger import Dinov3Tagger

        tagger = Dinov3Tagger.load(threads=args.threads)
        # Stamp what the LOADED weights actually were, never the file we read — a tagger
        # loaded some other way can then never write a row claiming a revision it did
        # not use (the rail clip_tag_backfill.py already applies to CLIP).
        write_identity = {**identity, "revision": tagger.revision}
        throttle = WriteThrottle(args.max_write_mb_per_hour)
        r2 = image_storage.R2Client.from_env(max_pool_connections=args.workers + 4)
        deadline = time.monotonic() + args.max_seconds if args.max_seconds else None

        written = failed = seen = 0
        after_id = 0
        stopped = "drained"
        while seen < args.limit:
            if deadline and time.monotonic() >= deadline:
                stopped = "time-budget"
                break
            rows = select_pending(
                conn, identity=identity, batch=min(args.chunk, args.limit - seen),
                shard=args.shard, shards=args.shards, after_id=after_id)
            if not rows:
                break
            seen += len(rows)
            after_id = max(r[0] for r in rows)

            t0 = time.monotonic()
            decoded, chunk_failed = _download_decode(r2, rows, args.workers)
            failed += chunk_failed
            chunk_written = 0
            if decoded:
                emb = tagger.embed([d[1] for d in decoded], args.batch_size)
                params = [
                    (image_id, write_identity["model"], write_identity["revision"],
                     write_identity["library"], write_identity["pooling"],
                     write_identity["resolution"], write_identity["preprocessing"],
                     write_identity["dtype"], _vec_str(emb[i]))
                    for i, (image_id, _img) in enumerate(decoded)
                ]
                with conn.cursor() as cur:
                    cur.executemany(_INSERT_SQL, params)
                chunk_written = len(params)
                written += chunk_written
            elapsed = time.monotonic() - t0

            # Progress is a Postgres fact, not a local counter: embedded_before was read
            # from the table and every increment is a committed row. The full count is
            # NOT re-read per chunk — it is a 10M-row scan — but it is re-read once at
            # the end, and `count(*) for this config / count(images)` answers "how far
            # along is it?" from outside the job at any moment.
            slept = throttle.pace(chunk_written, elapsed)
            LOG.info("DINOV3 progress embedded=%d/%d (%.2f%%) run_written=%d seen=%d/%d "
                     "failed=%d chunk_s=%.1f slept_s=%.1f",
                     embedded_before + written, total_images,
                     100.0 * (embedded_before + written) / total_images if total_images else 0.0,
                     written, seen, args.limit, failed, elapsed, slept)

        embedded_after = _scalar(conn, _EMBEDDED_COUNT_SQL, identity)

    LOG.info("DINOV3 done stop=%s run_written=%d failed=%d embedded=%d/%d (%.2f%%) "
             "throttle_slept_s=%.0f",
             stopped, written, failed, embedded_after, total_images,
             100.0 * embedded_after / total_images if total_images else 0.0,
             throttle.slept_s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
