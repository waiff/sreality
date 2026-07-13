"""Enrichment PR B — submit bazos description-enrichment batches to the
Anthropic Message Batches API (50% cheaper than synchronous enrichment).

Selects pending listings via the same predicate as
scripts.enrich_listing_descriptions._select_pending, builds one request per
listing via toolkit.bazos_enrichment.build_enrich_request +
provider.build_batch_request_params(...), submits them in size-bounded
chunks (every request embeds the enrichment system prompt + tool schema,
so a large unchunked submit risks the API's 256MB request cap), and
records each batch + its custom_id -> (sreality_id, snapshot_id) map in
listing_description_enrichment_batches /
listing_description_enrichment_batch_requests.

Listings already in an in-flight batch (a `pending` request row on a
non-terminal batch) are skipped, so overlapping submit runs don't
double-bill the same snapshot.

Results are picked up later by scripts.ingest_enrich_batch.

Usage (typically via .github/workflows/enrich_bazos_batch.yml):

    python -m scripts.submit_enrich_batch --source bazos --limit 2000

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

LOG = logging.getLogger("submit_enrich_batch")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="bazos")
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--max-age-days", type=int, default=0)
    parser.add_argument("--model", default=None)
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

    from api.providers.anthropic import AnthropicProvider
    from scripts.enrich_listing_descriptions import _select_pending
    from scripts.submit_condition_batch import (
        MAX_BATCH_BYTES,
        MAX_BATCH_REQUESTS,
        should_flush,
    )
    from toolkit.bazos_enrichment import DEFAULT_MODEL, build_enrich_request

    model = args.model or DEFAULT_MODEL
    LOG.info(
        "BATCH submit config source=%s model=%s limit=%d max_age_days=%d dry_run=%s",
        args.source, model, args.limit, args.max_age_days, args.dry_run,
    )

    provider = AnthropicProvider()
    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        pending = _select_pending(
            conn, source=args.source, model=model,
            max_age_days=args.max_age_days, limit=args.limit,
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

        items: list[tuple[str, dict[str, Any]]] = []
        mapping: list[tuple[str, int, int]] = []
        chunk_bytes = 0
        chunks = 0
        built = 0
        skipped = 0
        for sid in candidates:
            req = build_enrich_request(conn, sid, model=model)
            if req is None:
                skipped += 1
                continue
            params = provider.build_batch_request_params(
                system=req["system"],
                messages=req["messages"],
                tools=req["tools"],
                model=req["model"],
                tool_choice=req["tool_choice"],
                max_tokens=req["max_tokens"],
            )
            item_bytes = len(json.dumps(params, separators=(",", ":")))
            custom_id = f"s{sid}-snap{req['snapshot_id']}"
            if should_flush(
                n_items=len(items), chunk_bytes=chunk_bytes,
                next_item_bytes=item_bytes,
                max_requests=MAX_BATCH_REQUESTS, max_bytes=MAX_BATCH_BYTES,
            ):
                _submit_chunk(
                    conn, provider, items=items, mapping=mapping,
                    chunk_bytes=chunk_bytes, model=model, dry_run=args.dry_run,
                )
                chunks += 1
                items, mapping, chunk_bytes = [], [], 0
            items.append((custom_id, params))
            mapping.append((custom_id, sid, int(req["snapshot_id"])))
            chunk_bytes += item_bytes
            built += 1

        if built == 0:
            LOG.info("BATCH no requests built (skipped=%d); done", skipped)
            return 0

        if items:
            _submit_chunk(
                conn, provider, items=items, mapping=mapping,
                chunk_bytes=chunk_bytes, model=model, dry_run=args.dry_run,
            )
            chunks += 1

        LOG.info(
            "BATCH done requests=%d chunks=%d skipped=%d model=%s dry_run=%s",
            built, chunks, skipped, model, args.dry_run,
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
        conn, provider_batch_id=provider_batch_id, model=model, mapping=mapping,
    )
    LOG.info(
        "BATCH submitted provider_batch_id=%s requests=%d serialized=%.1fMB "
        "batch_id=%d model=%s",
        provider_batch_id, len(items), mb, batch_id, model,
    )


def _in_flight_sreality_ids(conn: Any) -> set[int]:
    sql = (
        "SELECT DISTINCT r.sreality_id "
        "FROM listing_description_enrichment_batch_requests r "
        "JOIN listing_description_enrichment_batches b ON b.id = r.batch_id "
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
    mapping: list[tuple[str, int, int]],
) -> int:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO listing_description_enrichment_batches "
            "(provider, provider_batch_id, model, request_count, status) "
            "VALUES ('anthropic', %s, %s, %s, 'submitted') RETURNING id",
            (provider_batch_id, model, len(mapping)),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                "INSERT into listing_description_enrichment_batches returned no id"
            )
        batch_id = int(row[0])
        cur.executemany(
            "INSERT INTO listing_description_enrichment_batch_requests "
            "(batch_id, custom_id, sreality_id, snapshot_id) "
            "VALUES (%s, %s, %s, %s)",
            [(batch_id, custom_id, sid, snapshot_id)
             for custom_id, sid, snapshot_id in mapping],
        )
    return batch_id


if __name__ == "__main__":
    sys.exit(main())
