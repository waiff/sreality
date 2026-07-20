"""Flush the dedup-vision batch spool (dedup-cost-reduction.md §4.1 / the
overhaul doc's engine-fed deferral, superseding the old speculative warmer).

The ENGINE decides what to warm now, not this script: a sweep lane
(scripts/dedup_engine.py — full street scan, geo, byt-geo, candidate drain)
that would make a COLD classify/compare/site-plan/floor-plan call instead
builds the exact request and spools it — one dedup_batch_requests row with
batch_id NULL, request_params holding the already-built provider-shaped body
(toolkit.dedup_batch_defer.enqueue_deferred_request) — and defers the pair.
Dirty/realtime lanes stay synchronous; they never spool. Gated by
app_settings.dedup_engine_batch_defer_enabled (default off).

This script's only job is to FLUSH that spool: read every unsubmitted
request (batch_id IS NULL), chunk them per-provider under the size/count
caps, submit each chunk as one provider Batch API call (50% off), record a
dedup_batches row, and attach the batch_id back onto the flushed requests.
It never decides what to warm and never calls the LLM synchronously.

Because the engine is the one choosing what to defer, selection identity
holds by construction — no second process re-derives (and inevitably
diverges from) the engine's own work-list, unlike the retired collect()
funnel this replaces.

Results are picked up by scripts.ingest_dedup_batch (unchanged): it reads
the dedup_batches rows this script inserts exactly as it always has.

Usage (typically via .github/workflows/dedup_batches.yml):

    python -m scripts.submit_dedup_batch --max-requests 3000

Required env: SUPABASE_DB_URL + at least one provider key (ANTHROPIC_API_KEY
and/or OPENAI_API_KEY) — whichever the spooled requests' models resolve to.
--dry-run needs neither provider key (R2 access was already spent by the
engine at defer time; this script never touches R2).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

from toolkit.batch_submit import (
    MAX_BATCH_BYTES,
    MAX_BATCH_REQUESTS,
    should_flush,
    submit_chunk_with_retry,
)

LOG = logging.getLogger("submit_dedup_batch")


@dataclass
class _SpooledReq:
    id: int
    custom_id: str
    kind: str
    model: str
    sreality_id_a: int
    sreality_id_b: int | None
    room_type: str | None
    image_ids: list[int] | None
    request_params: dict[str, Any]


def _fetch_spooled(conn: Any, *, limit: int) -> list[_SpooledReq]:
    sql = (
        "SELECT id, custom_id, kind, model, sreality_id_a, sreality_id_b, "
        "room_type, image_ids, request_params "
        "FROM dedup_batch_requests WHERE batch_id IS NULL "
        "ORDER BY queued_at ASC LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (limit,))
        rows = cur.fetchall()
    return [
        _SpooledReq(
            id=int(r[0]), custom_id=r[1], kind=r[2], model=r[3],
            sreality_id_a=int(r[4]), sreality_id_b=int(r[5]) if r[5] is not None else None,
            room_type=r[6], image_ids=list(r[7]) if r[7] is not None else None,
            request_params=r[8],
        )
        for r in rows
    ]


def _skip_no_provider(conn: Any, ids: list[int]) -> None:
    """A spooled request whose model no longer resolves to a batch-capable
    provider (e.g. a settings flip to gemini/qwen after it was spooled) can
    never flush — mark it 'skipped' so it stops being re-selected every run
    (it was never billed, so nothing to reconcile)."""
    if not ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE dedup_batch_requests SET status = 'skipped' WHERE id = ANY(%s)",
            (ids,),
        )


def _insert_batch_and_attach(
    conn: Any, provider_batch_id: str, provider: str, chunk: list[_SpooledReq],
) -> int:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO dedup_batches (provider, provider_batch_id, request_count, status) "
            "VALUES (%s, %s, %s, 'submitted') RETURNING id",
            (provider, provider_batch_id, len(chunk)),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT into dedup_batches returned no id")
        batch_id = int(row[0])
        cur.execute(
            "UPDATE dedup_batch_requests SET batch_id = %s WHERE id = ANY(%s)",
            (batch_id, [r.id for r in chunk]),
        )
    return batch_id


def _kind_counts(chunk: list[_SpooledReq]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in chunk:
        out[r.kind] = out.get(r.kind, 0) + 1
    return out


def flush(
    conn: Any, providers: dict[str, Any], *, max_requests: int, dry_run: bool,
) -> dict[str, int]:
    """Submit whatever's spooled, chunked per-provider by size/count caps."""
    stats = {
        "flushed": 0, "batches": 0, "skipped_no_provider": 0, "submit_failures": 0,
    }
    spooled = _fetch_spooled(conn, limit=max_requests)
    if not spooled:
        return stats

    from api.llm_client import provider_for_model

    chunks: dict[str, list[_SpooledReq]] = {}
    chunk_bytes: dict[str, int] = {}

    def _flush_chunk(pname: str) -> None:
        chunk = chunks.get(pname) or []
        if not chunk:
            return
        mb = chunk_bytes.get(pname, 0) / (1024 * 1024)
        if dry_run:
            LOG.info("BATCH dry-run chunk provider=%s requests=%d serialized=%.1fMB kinds=%s",
                      pname, len(chunk), mb, _kind_counts(chunk))
            stats["flushed"] += len(chunk)
            stats["batches"] += 1
            chunks[pname] = []
            chunk_bytes[pname] = 0
            return
        items = [(r.custom_id, r.request_params) for r in chunk]
        provider_batch_id = submit_chunk_with_retry(providers[pname], items, label="dedup")
        if provider_batch_id is None:
            # Nothing was inserted/attached, so these requests stay spooled
            # (batch_id still NULL) and the next scheduled flush retries them.
            stats["submit_failures"] += 1
            chunks[pname] = []
            chunk_bytes[pname] = 0
            return
        batch_id = _insert_batch_and_attach(conn, provider_batch_id, pname, chunk)
        LOG.info(
            "BATCH submitted provider=%s provider_batch_id=%s requests=%d serialized=%.1fMB "
            "batch_id=%d kinds=%s",
            pname, provider_batch_id, len(chunk), mb, batch_id, _kind_counts(chunk),
        )
        stats["flushed"] += len(chunk)
        stats["batches"] += 1
        chunks[pname] = []
        chunk_bytes[pname] = 0

    no_provider_ids: list[int] = []
    for req in spooled:
        pname = provider_for_model(req.model)
        if providers.get(pname) is None:
            no_provider_ids.append(req.id)
            stats["skipped_no_provider"] += 1
            continue
        item_bytes = len(json.dumps(req.request_params, separators=(",", ":")))
        chunk = chunks.setdefault(pname, [])
        if should_flush(
            n_items=len(chunk), chunk_bytes=chunk_bytes.get(pname, 0),
            next_item_bytes=item_bytes,
            max_requests=MAX_BATCH_REQUESTS, max_bytes=MAX_BATCH_BYTES,
        ):
            _flush_chunk(pname)
            chunk = chunks.setdefault(pname, [])
        chunk.append(req)
        chunk_bytes[pname] = chunk_bytes.get(pname, 0) + item_bytes

    for pname in list(chunks.keys()):
        _flush_chunk(pname)
    if not dry_run:
        _skip_no_provider(conn, no_provider_ids)

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-requests", type=int, default=MAX_BATCH_REQUESTS * 5,
        help="Maximum spooled requests to flush this run (spread across chunks).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be submitted without calling the provider.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    if not args.dry_run and not (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")
    ):
        print(
            "ERROR: no provider key set; need ANTHROPIC_API_KEY and/or OPENAI_API_KEY.",
            file=sys.stderr,
        )
        return 2

    import psycopg

    from api.providers.anthropic import AnthropicProvider
    from api.providers.openai import OpenAIProvider

    # Batch-capable providers, keyed by name. Each spooled request routes to the
    # provider its model resolves to (llm_client.provider_for_model) — whatever the
    # engine was configured with at defer time. Keys are lazy, so an unused
    # provider's missing secret costs nothing until a request needs it.
    providers = {"anthropic": AnthropicProvider(), "openai": OpenAIProvider()}
    LOG.info("BATCH flush config max_requests=%d dry_run=%s", args.max_requests, args.dry_run)

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        stats = flush(conn, providers, max_requests=args.max_requests, dry_run=args.dry_run)

    LOG.info(
        "BATCH done flushed=%d batches=%d skipped_no_provider=%d submit_failures=%d dry_run=%s",
        stats["flushed"], stats["batches"], stats["skipped_no_provider"],
        stats["submit_failures"], args.dry_run,
    )
    if stats["submit_failures"] and not stats["batches"]:
        # Every flush failed (dead key / provider outage / bad config): a red run is
        # the only signal — with retries swallowing per-chunk errors, exit 0 here
        # would disguise a fully-dead flush lane as a quiet no-op.
        LOG.error("BATCH flush produced 0 batches with %d failed flushes", stats["submit_failures"])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
