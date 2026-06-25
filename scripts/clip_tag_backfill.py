"""Backfill image_clip_tags + image_clip_embeddings: the self-hosted CLIP tagger's
room/plot tag + 512-d embedding per stored image.

Selects images not yet CLIP-tagged via the in-table marker images.clip_tagged_at
(migration 232) — priority kraje first (app_settings.clip_tagging_priority_region_ids,
or a one-off --region-id), then a global newest-first sweep — downloads bytes from R2 on
a worker pool, tags + embeds them, upserts one row per image, and stamps the marker so
the image drops out of the needs-clip partial index. Idempotent + resumable + shardable
(image_id % shards), so it parallelises like images.yml. A FREE replacement for the paid
room classifier on the coarse dedup-relevant tags, and the first tagger for
dum/pozemek/komercni. No-op (exit 0) if R2 env vars are missing.

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

# "Pending" = a stored image not yet CLIP-tagged. The source of truth is the in-table
# marker images.clip_tagged_at (migration 232), set in the same write that upserts the
# tag — NOT a cross-table anti-join against image_clip_tags, which forced a full seq-scan
# + sort of the ~5.2M-row images table every run and intermittently blew the pooler's
# 2-min statement timeout (the failed shards). The marker is backed by partial indexes
# (images_needs_clip_{id,sid}_idx) that become selective as coverage rises, so the SELECT
# trends to sub-second. During the initial bulk backfill (most images still untagged) the
# partial index is NOT selective and a scan is unavoidable, so the SELECT runs under a
# generous batch statement_timeout (the 2-min pooler default is an OLTP limit, wrong for a
# backfill). Region scope lets the operator make one kraj's dedup ready first
# (app_settings.clip_tagging_priority_region_ids, ordered) — drained before the global
# newest-first fallback that covers every remaining region + the region-NULL (foreign) tail.
_SELECT_REGION = """
    SELECT i.id, i.storage_path, (l.is_active IS TRUE) AS is_active
    FROM listings l
    JOIN images i ON i.sreality_id = l.sreality_id
    WHERE l.region_id = %(region)s
      AND i.clip_tagged_at IS NULL AND i.storage_path IS NOT NULL
      AND (%(shards)s = 1 OR i.id %% %(shards)s = %(shard)s)
    ORDER BY (l.is_active IS TRUE) DESC, i.id DESC
    LIMIT %(limit)s
"""
_SELECT_GLOBAL = """
    SELECT i.id, i.storage_path, (l.is_active IS TRUE) AS is_active
    FROM images i
    LEFT JOIN listings l ON l.sreality_id = i.sreality_id
    WHERE i.clip_tagged_at IS NULL AND i.storage_path IS NOT NULL
      AND (%(shards)s = 1 OR i.id %% %(shards)s = %(shard)s)
    ORDER BY i.id DESC
    LIMIT %(limit)s
"""

# Bridges the initial bulk-backfill scan; once coverage is high the marker partial index
# makes the SELECT sub-second and this margin is never approached. Well under the 55-min job.
SELECT_TIMEOUT_MS = 300_000

# Set the marker in the SAME write that records the tags, so a tagged image drops out of
# the partial index immediately (idempotent; only flips NULL -> now()).
_MARK_SQL = "UPDATE images SET clip_tagged_at = now() WHERE id = ANY(%s) AND clip_tagged_at IS NULL"

_UPSERT_SQL = """
    INSERT INTO image_clip_tags (image_id, model, fine_tag, logical_tag, confidence, render_score)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (image_id, model) DO UPDATE
      SET fine_tag = EXCLUDED.fine_tag, logical_tag = EXCLUDED.logical_tag,
          confidence = EXCLUDED.confidence, render_score = EXCLUDED.render_score,
          tagged_at = now()
"""

# Embeddings (for the cosine recall tier) stored ACTIVE-listing-only — that bounds
# the footprint to the dedup-relevant set (the cosine tier never scores inactive
# pairs). pgvector parses the text '[f,f,...]' form.
_UPSERT_EMB_SQL = """
    INSERT INTO image_clip_embeddings (image_id, model, embedding)
    VALUES (%s, %s, %s::vector)
    ON CONFLICT (image_id, model) DO UPDATE SET embedding = EXCLUDED.embedding
"""


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _vec_str(row) -> str:
    """A normalized embedding row -> pgvector text form '[f,f,...]'."""
    return "[" + ",".join(f"{x:.6f}" for x in row.tolist()) + "]"


def _download_decode(r2: image_storage.R2Client, rows: list, workers: int):
    """Returns (decoded, terminal): decoded = [(image_id, RGB image)] that tagged
    successfully; terminal = [image_id] whose STORED bytes won't ever decode (corrupt /
    non-image — e.g. a video cover that slipped past the download reject-list). The
    caller marks BOTH so a permanently-undecodable image leaves the needs-clip partial
    index instead of being re-fetched + re-failed every run (the image-download pipeline
    terminal-marks the same way). A DOWNLOAD exception is treated as TRANSIENT (R2 blip)
    and left unmarked to retry — mirroring how the download pipeline distinguishes a gone
    object from a transient miss."""
    from PIL import Image  # base dep

    def _one(row):
        image_id, key = row[0], row[1]
        try:
            data = r2.download_bytes(key)
        except Exception:  # noqa: BLE001 - transient R2 error: retry on the next run
            return image_id, None, False
        try:
            return image_id, Image.open(io.BytesIO(data)).convert("RGB"), False
        except Exception:  # noqa: BLE001 - stored bytes won't decode: terminal, mark it
            return image_id, None, True

    decoded: list = []
    terminal: list = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for image_id, img, is_terminal in pool.map(_one, rows):
            if img is not None:
                decoded.append((image_id, img))
            elif is_terminal:
                terminal.append(image_id)
    return decoded, terminal


def _priority_region_ids(conn, cli_region: int | None) -> list[int]:
    """Ordered kraj ids to drain before the global fallback. A CLI --region-id wins (ad-hoc
    dispatch); otherwise the operator's app_settings.clip_tagging_priority_region_ids list
    (empty = no priority, straight to the global newest-first sweep)."""
    if cli_region is not None:
        return [cli_region]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM app_settings WHERE key = 'clip_tagging_priority_region_ids'"
        )
        row = cur.fetchone()
    val = row[0] if row and row[0] is not None else None
    if isinstance(val, list):
        return [int(x) for x in val]
    return []


def _select_pending(conn, *, limit: int, shards: int, shard: int,
                    priority_regions: list[int]) -> tuple[list, str]:
    """The run's pending images: priority kraje first (in order), then the global
    newest-first fallback for everything else, up to `limit`. One read transaction with a
    batch statement_timeout (SET LOCAL) so the bulk-phase scan can't be killed by the
    OLTP pooler default. Returns (rows, phase_label)."""
    base = {"shards": shards, "shard": shard}
    rows: list[tuple[int, str, bool]] = []
    phases: list[str] = []
    with conn.transaction(), conn.cursor() as cur:
        # SET is a utility statement — it can't take a bound parameter ($1), so the
        # (module-constant int) value is interpolated, not passed as %s.
        cur.execute(f"SET LOCAL statement_timeout = {int(SELECT_TIMEOUT_MS)}")
        for region in priority_regions:
            if len(rows) >= limit:
                break
            cur.execute(_SELECT_REGION,
                        {**base, "region": region, "limit": limit - len(rows)})
            got = [(r[0], r[1], r[2]) for r in cur.fetchall()]
            if got:
                rows += got
                phases.append(f"r{region}:{len(got)}")
        if len(rows) < limit:
            cur.execute(_SELECT_GLOBAL, {**base, "limit": limit - len(rows)})
            got = [(r[0], r[1], r[2]) for r in cur.fetchall()]
            if got:
                rows += got
                phases.append(f"global:{len(got)}")
    # The global fallback has no region exclusion, so a just-returned priority-region
    # image can reappear there — de-dup by image_id (keep first) so it isn't downloaded
    # + CLIP-encoded twice in one run.
    seen: set[int] = set()
    deduped = [r for r in rows if not (r[0] in seen or seen.add(r[0]))]
    return deduped, ("+".join(phases) or "empty")


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
    p.add_argument("--region-id", type=int, default=None,
                   help="One-off: drain this kraj first, then the global fallback. "
                        "Overrides app_settings.clip_tagging_priority_region_ids. "
                        "Unset = use that operator priority list (empty = global only).")
    p.add_argument("--categories", type=str, default="",
                   help="Deprecated + ignored (the marker selector tags all categories).")
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

    model = load_taxonomy()["model"]  # for the tag/embedding upsert + the log; no torch load

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        priority = _priority_region_ids(conn, args.region_id)
        rows, phase = _select_pending(
            conn, limit=args.limit, shards=args.shards, shard=args.shard,
            priority_regions=priority)
        active = {r[0]: r[2] for r in rows}  # store embeddings for active only
        LOG.info("CLIP_TAG pending=%d phase=%s shard=%d/%d model=%s dry_run=%s",
                 len(rows), phase, args.shard, args.shards, model, args.dry_run)
        if args.dry_run or not rows:
            return 0

        tagger = Tagger.load(args.threads)  # loads the model once
        r2 = image_storage.R2Client.from_env(max_pool_connections=args.workers + 4)
        written = embedded = errors = terminal_n = 0
        for chunk in _chunks(rows, args.chunk):
            decoded, terminal = _download_decode(r2, chunk, args.workers)
            errors += len(chunk) - len(decoded)
            terminal_n += len(terminal)
            ids = [d[0] for d in decoded]
            # Mark successes AND terminally-undecodable images — both leave the partial
            # index. Skip only when the whole chunk was a transient download miss (mark
            # nothing → those retry next run). Idempotent: _MARK_SQL flips NULL->now() once.
            mark_ids = ids + terminal
            if not mark_ids:
                continue
            tag_params = []
            emb_params = []
            if decoded:
                emb = tagger.embed([d[1] for d in decoded], args.batch_size)
                results = tagger.tags_from_emb(emb)
                tag_params = [
                    (image_id, model, r.fine_tag, r.logical_tag, r.confidence, r.render_score)
                    for image_id, r in zip(ids, results)
                ]
                emb_params = [
                    (image_id, model, _vec_str(emb[i]))
                    for i, image_id in enumerate(ids) if active.get(image_id)
                ]
            with conn.cursor() as cur:
                if tag_params:
                    cur.executemany(_UPSERT_SQL, tag_params)
                if emb_params:
                    cur.executemany(_UPSERT_EMB_SQL, emb_params)
                cur.execute(_MARK_SQL, (mark_ids,))  # drop from the needs-clip partial index
            written += len(tag_params)
            embedded += len(emb_params)
            LOG.info("CLIP_TAG progress=%d/%d embedded=%d errors=%d terminal=%d",
                     written, len(rows), embedded, errors, terminal_n)

    LOG.info("CLIP_TAG done written=%d embedded=%d errors=%d terminal=%d",
             written, embedded, errors, terminal_n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
