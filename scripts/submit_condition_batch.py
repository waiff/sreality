"""Phase 1.8b — submit condition-scoring batches to the Anthropic Message
Batches API (50% cheaper than synchronous scoring, async).

Selects active listings whose latest snapshot has no condition-score row
(same predicate as scripts.backfill_condition_scores), builds one request
per listing, submits them in size-bounded chunks (every request embeds the
~61KB shared system prompt, so a large unchunked submit blows past the
API's 256MB request cap — HTTP 413), and records each batch + its
custom_id → (sreality_id, snapshot_id) map in
condition_score_batches / condition_score_batch_requests. Multiple chunks
per run mean multiple condition_score_batches rows; the ingester already
walks every in-flight row.

Listings already in an in-flight batch (a `pending` request row on a
non-terminal batch) are skipped, so overlapping submit runs don't
double-bill the same snapshot. Before selection,
toolkit.condition_scoring.propagate_condition_levels copies existing
genuine scores to cross-portal siblings of the same property, so a
duplicate never re-bills the LLM.

Results are picked up later by scripts.ingest_condition_batch.

Usage (typically via .github/workflows/condition_score_batches.yml):

    python -m scripts.submit_condition_batch \\
        --region-ids 27,43,108 \\
        --limit 2000

Required env: SUPABASE_DB_URL, ANTHROPIC_API_KEY (the latter only when
not --dry-run).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

LOG = logging.getLogger("submit_condition_batch")

# The Message Batches API rejects request bodies over 256MB; flush with a
# wide safety margin so the per-request system prompt can't sum past it.
MAX_BATCH_BYTES = 150 * 1024 * 1024
MAX_BATCH_REQUESTS = 2000


def should_flush(
    *,
    n_items: int,
    chunk_bytes: int,
    next_item_bytes: int,
    max_requests: int = MAX_BATCH_REQUESTS,
    max_bytes: int = MAX_BATCH_BYTES,
) -> bool:
    """True when the next request must start a new batch (count or byte cap)."""
    if n_items == 0:
        return False
    return n_items >= max_requests or chunk_bytes + next_item_bytes > max_bytes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region-ids", default="")
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--n-images", type=int, default=0)
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Build requests and print the count without submitting a batch.",
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
    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 2

    import psycopg

    from api.llm_client import LLMClient
    from api.providers.anthropic import AnthropicProvider
    from scripts.backfill_condition_scores import _parse_region_ids, _select_pending
    from toolkit.condition_scoring import (
        build_scoring_context,
        build_scoring_request,
        propagate_condition_levels,
        resolve_snapshot,
    )

    region_ids = _parse_region_ids(args.region_ids)
    LOG.info(
        "BATCH submit config region_ids=%s limit=%d n_images=%d "
        "max_age_days=%d dry_run=%s",
        region_ids or "from-settings", args.limit, args.n_images,
        args.max_age_days, args.dry_run,
    )

    provider = AnthropicProvider()
    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        llm_client = LLMClient(conn, providers={"anthropic": provider})

        if not args.dry_run:
            # Copy already-paid scores to cross-portal siblings first, so the
            # selection below never re-bills a property a sibling already covers.
            reused = propagate_condition_levels(conn)
            LOG.info("PROPAGATE reused=%d", reused)

        pending = _select_pending(
            conn,
            region_ids=region_ids,
            max_age_days=args.max_age_days,
            limit=args.limit,
        )
        in_flight = _in_flight_sreality_ids(conn)
        candidates = [sid for sid in pending if sid not in in_flight]
        LOG.info(
            "BATCH selected pending=%d in_flight_skipped=%d candidates=%d",
            len(pending), len(pending) - len(candidates), len(candidates),
        )
        if not candidates:
            LOG.info("BATCH nothing to submit; done")
            return 0

        context = build_scoring_context(conn, llm_client)
        model = context["model"]

        items: list[tuple[str, dict[str, Any]]] = []
        mapping: list[tuple[str, int, int]] = []
        chunk_bytes = 0
        chunks = 0
        built = 0
        for sid in candidates:
            snap = resolve_snapshot(conn, sid, None)
            if snap is None:
                LOG.warning("BATCH skip sreality_id=%d (no snapshot)", sid)
                continue
            req = build_scoring_request(
                conn, llm_client, sreality_id=sid, snapshot=snap,
                n_images=args.n_images, context=context,
            )
            custom_id = f"s{sid}-snap{snap['id']}"
            params = provider.build_batch_request_params(
                system=req["system"],
                messages=req["messages"],
                tools=req["tools"],
                model=req["model"],
            )
            item_bytes = len(json.dumps(params, separators=(",", ":")))
            if should_flush(
                n_items=len(items), chunk_bytes=chunk_bytes,
                next_item_bytes=item_bytes,
            ):
                _submit_chunk(
                    conn, provider, items=items, mapping=mapping,
                    chunk_bytes=chunk_bytes, model=model,
                    n_images=args.n_images, dry_run=args.dry_run,
                )
                chunks += 1
                items, mapping, chunk_bytes = [], [], 0
            items.append((custom_id, params))
            mapping.append((custom_id, sid, int(snap["id"])))
            chunk_bytes += item_bytes
            built += 1

        if built == 0:
            LOG.info("BATCH no requests built; done")
            return 0

        if items:
            _submit_chunk(
                conn, provider, items=items, mapping=mapping,
                chunk_bytes=chunk_bytes, model=model,
                n_images=args.n_images, dry_run=args.dry_run,
            )
            chunks += 1

        LOG.info(
            "BATCH done requests=%d chunks=%d model=%s dry_run=%s",
            built, chunks, model, args.dry_run,
        )
    return 0


def _submit_chunk(
    conn: Any,
    provider: Any,
    *,
    items: list[tuple[str, dict[str, Any]]],
    mapping: list[tuple[str, int, int]],
    chunk_bytes: int,
    model: str,
    n_images: int,
    dry_run: bool,
) -> None:
    mb = chunk_bytes / (1024 * 1024)
    if dry_run:
        LOG.info(
            "BATCH dry-run chunk requests=%d serialized=%.1fMB model=%s",
            len(items), mb, model,
        )
        for custom_id, sid, snapshot_id in mapping[:5]:
            LOG.info("BATCH sample %s -> sreality_id=%d snapshot_id=%d",
                     custom_id, sid, snapshot_id)
        return
    provider_batch_id = provider.submit_batch(items)
    batch_id = _insert_batch(
        conn,
        provider_batch_id=provider_batch_id,
        model=model,
        n_images=n_images,
        mapping=mapping,
    )
    LOG.info(
        "BATCH submitted provider_batch_id=%s requests=%d serialized=%.1fMB "
        "batch_id=%d model=%s",
        provider_batch_id, len(items), mb, batch_id, model,
    )


def _in_flight_sreality_ids(conn: Any) -> set[int]:
    sql = (
        "SELECT DISTINCT r.sreality_id "
        "FROM condition_score_batch_requests r "
        "JOIN condition_score_batches b ON b.id = r.batch_id "
        "WHERE b.status IN ('submitted', 'ended') AND r.status = 'pending'"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
        return {int(row[0]) for row in cur.fetchall()}


def _insert_batch(
    conn: Any,
    *,
    provider_batch_id: str,
    model: str,
    n_images: int,
    mapping: list[tuple[str, int, int]],
) -> int:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO condition_score_batches "
            "(provider, provider_batch_id, model, n_images, request_count, status) "
            "VALUES ('anthropic', %s, %s, %s, %s, 'submitted') RETURNING id",
            (provider_batch_id, model, n_images, len(mapping)),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT into condition_score_batches returned no id")
        batch_id = int(row[0])
        cur.executemany(
            "INSERT INTO condition_score_batch_requests "
            "(batch_id, custom_id, sreality_id, snapshot_id) "
            "VALUES (%s, %s, %s, %s)",
            [(batch_id, custom_id, sid, snapshot_id)
             for custom_id, sid, snapshot_id in mapping],
        )
    return batch_id


if __name__ == "__main__":
    sys.exit(main())
