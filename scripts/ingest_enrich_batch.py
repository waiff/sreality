"""Enrichment PR B — ingest completed Anthropic bazos description-enrichment
batches.

Polls every non-terminal row in listing_description_enrichment_batches. For
a batch the provider reports as `ended`, streams the results and, per
request:

  * looks up (sreality_id, snapshot_id) from the custom_id map,
  * pulls the `record_listing` tool call off the result (missing = the
    negative-cache `no_extraction` case, same as the synchronous path),
  * re-resolves the listing's CURRENT gap columns (toolkit.bazos_enrichment
    .resolve_current) — not the possibly-hours-stale state captured at
    submit time — and skips persisting when the mapped snapshot is no
    longer the latest one,
  * persists via toolkit.bazos_enrichment.persist_enrich_result (same cache
    row + gap-column UPDATE as the synchronous enricher),
  * records one llm_calls row at the 50% batch-discounted cost.

Idempotent: only `pending` request rows are processed; persist_enrich_result's
ON CONFLICT DO NOTHING cache write makes re-writes safe, so a re-run after a
partial ingest finishes the rest.

Usage (typically via .github/workflows/enrich_bazos_batch.yml):

    python -m scripts.ingest_enrich_batch

Required env: SUPABASE_DB_URL, ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

LOG = logging.getLogger("ingest_enrich_batch")

# Anthropic Message Batches bills token usage at 50% of standard prices.
BATCH_DISCOUNT = 0.5

_CALLED_FOR = "enrich_listing_description"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-batches", type=int, default=20,
        help="Maximum number of in-flight batches to process this run.",
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
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 2

    import psycopg

    from api.llm_client import LLMClient
    from api.providers.anthropic import AnthropicProvider
    from api.providers.openai import OpenAIProvider

    providers = {"anthropic": AnthropicProvider(), "openai": OpenAIProvider()}
    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        llm_client = LLMClient(conn, providers=providers)
        batches = _in_flight_batches(conn, limit=args.max_batches)
        LOG.info("INGEST in_flight_batches=%d", len(batches))
        if not batches:
            LOG.info("INGEST nothing to do; done")
            return 0

        for batch in batches:
            try:
                _process_batch(conn, providers, llm_client, batch)
            except Exception as exc:  # noqa: BLE001 — one batch's provider hiccup mustn't stall the rest
                LOG.error("INGEST batch_id=%s failed: %s", batch.get("id"), exc)
    return 0


def _in_flight_batches(conn: Any, *, limit: int) -> list[dict[str, Any]]:
    sql = (
        "SELECT id, provider_batch_id, model, provider "
        "FROM listing_description_enrichment_batches "
        "WHERE status IN ('submitted', 'ended') "
        "ORDER BY submitted_at ASC LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (limit,))
        return [
            {"id": r[0], "provider_batch_id": r[1], "model": r[2],
             "provider": r[3] or "anthropic"}
            for r in cur.fetchall()
        ]


def _process_batch(
    conn: Any,
    providers: dict[str, Any],
    llm_client: Any,
    batch: dict[str, Any],
) -> int:
    """Returns the number of results persisted."""
    from api.providers import compute_cost_usd

    batch_id = int(batch["id"])
    provider_batch_id = batch["provider_batch_id"]
    model = batch["model"]
    pname = batch.get("provider") or "anthropic"
    provider = providers.get(pname)
    if provider is None:
        LOG.error("INGEST batch_id=%d unknown provider %r; skipping", batch_id, pname)
        return 0

    status = provider.poll_batch(provider_batch_id)
    LOG.info(
        "INGEST batch_id=%d provider_batch_id=%s status=%s counts=%s",
        batch_id, provider_batch_id, status.raw_status, status.counts,
    )
    if not status.ended:
        _update_batch_counts(conn, batch_id, status)
        return 0

    _mark_batch_ended(conn, batch_id, status)
    mapping = _pending_requests(conn, batch_id)
    if not mapping:
        LOG.info("INGEST batch_id=%d no pending requests; marking ingested", batch_id)
        _finalize_batch(conn, batch_id, scored=0, errored=0, cost=0.0)
        return 0

    price = provider.price_for(model)
    scored = 0
    errored = 0
    cost_total = 0.0
    for item in provider.iter_batch_results(provider_batch_id):
        req = mapping.get(item.custom_id)
        if req is None:
            continue  # already ingested or unknown custom_id
        sreality_id = req["sreality_id"]
        snapshot_id = req["snapshot_id"]

        if item.status != "succeeded" or item.completion is None:
            errored += 1
            _mark_request(conn, req["id"], "errored", (item.error or item.status)[:500])
            continue

        cost = _ingest_one(
            conn, llm_client, compute_cost_usd, price,
            completion=item.completion, model=model, provider=pname,
            sreality_id=sreality_id, snapshot_id=snapshot_id,
            request_id=req["id"],
        )
        if cost is None:
            errored += 1
        else:
            scored += 1
            cost_total += cost

    _finalize_batch(conn, batch_id, scored=scored, errored=errored, cost=cost_total)
    LOG.info(
        "INGEST batch_id=%d done scored=%d errored=%d cost=$%.4f",
        batch_id, scored, errored, cost_total,
    )
    return scored


def _ingest_one(
    conn: Any,
    llm_client: Any,
    compute_cost_usd: Any,
    price: Any,
    *,
    completion: Any,
    model: str,
    provider: str,
    sreality_id: int,
    snapshot_id: int,
    request_id: int,
) -> float | None:
    """Persist one succeeded result. Returns its cost, or None on failure."""
    from toolkit.bazos_enrichment import persist_enrich_result, resolve_current

    current = resolve_current(conn, sreality_id, snapshot_id)
    if current is None:
        _mark_request(conn, request_id, "errored", "snapshot no longer current")
        return None

    extraction = next(
        (tc.input for tc in completion.tool_calls if tc.name == "record_listing"),
        None,
    )

    cost = round(
        compute_cost_usd(price=price, model=model, usage=completion.usage)
        * BATCH_DISCOUNT,
        6,
    )
    llm_call_id = llm_client.record_external_call(
        called_for=_CALLED_FOR,
        provider=provider,
        model=model,
        usage=completion.usage,
        cost_usd=cost,
    )
    persist_enrich_result(
        conn,
        sreality_id=sreality_id,
        snapshot_id=snapshot_id,
        current=current,
        extraction=extraction,
        model=model,
        llm_call_id=llm_call_id,
        cost_usd=cost,
    )
    _mark_request(conn, request_id, "scored", None)
    return cost


def _pending_requests(conn: Any, batch_id: int) -> dict[str, dict[str, Any]]:
    sql = (
        "SELECT id, custom_id, sreality_id, snapshot_id "
        "FROM listing_description_enrichment_batch_requests "
        "WHERE batch_id = %s AND status = 'pending'"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (batch_id,))
        return {
            r[1]: {"id": int(r[0]), "sreality_id": int(r[2]), "snapshot_id": int(r[3])}
            for r in cur.fetchall()
        }


def _mark_request(conn: Any, request_id: int, status: str, error: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE listing_description_enrichment_batch_requests "
            "SET status = %s, error = %s WHERE id = %s",
            (status, error, request_id),
        )


def _update_batch_counts(conn: Any, batch_id: int, status: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE listing_description_enrichment_batches "
            "SET succeeded_count = %s, errored_count = %s WHERE id = %s",
            (status.counts.get("succeeded"), status.counts.get("errored"), batch_id),
        )


def _mark_batch_ended(conn: Any, batch_id: int, status: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE listing_description_enrichment_batches "
            "SET status = 'ended', ended_at = COALESCE(ended_at, now()), "
            "    succeeded_count = %s, errored_count = %s "
            "WHERE id = %s",
            (status.counts.get("succeeded"), status.counts.get("errored"), batch_id),
        )


def _finalize_batch(
    conn: Any, batch_id: int, *, scored: int, errored: int, cost: float,
) -> None:
    """Mark a batch ingested once no pending requests remain."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM listing_description_enrichment_batch_requests "
            "WHERE batch_id = %s AND status = 'pending'",
            (batch_id,),
        )
        remaining = int(cur.fetchone()[0])
        new_status = "ingested" if remaining == 0 else "ended"
        cur.execute(
            "UPDATE listing_description_enrichment_batches "
            "SET status = %s, "
            "    scored_count = COALESCE(scored_count, 0) + %s, "
            "    ingest_error_count = COALESCE(ingest_error_count, 0) + %s, "
            "    total_cost_usd = COALESCE(total_cost_usd, 0) + %s, "
            "    ingested_at = CASE WHEN %s = 'ingested' THEN now() ELSE ingested_at END "
            "WHERE id = %s",
            (new_status, scored, errored, cost, new_status, batch_id),
        )


if __name__ == "__main__":
    sys.exit(main())
