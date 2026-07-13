"""Ingest completed Anthropic dedup-vision batches.

Polls every non-terminal row in dedup_batches. For a batch the provider reports
as `ended`, streams the results and, per request, routes by `kind` to the owning
toolkit module's persist helper — writing the SAME cache row the synchronous
dedup tool would write:

  * classify  -> toolkit.image_classification.persist_room_classifications
  * compare   -> toolkit.visual_match.persist_visual_match
  * site_plan -> toolkit.visual_match.persist_site_plan_match

and records one llm_calls row per request at the 50% batch-discounted cost. The
lane does NOT merge — the daily dedup_engine.yml replay reads these warm caches
and performs the merges.

Idempotent: only `pending` request rows are processed; every cache write is keyed
(image_id / pair+room / pair) with ON CONFLICT upsert, so a re-run after a partial
ingest safely finishes the rest.

Usage (typically via .github/workflows/dedup_batches.yml):

    python -m scripts.ingest_dedup_batch

Required env: SUPABASE_DB_URL, ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

LOG = logging.getLogger("ingest_dedup_batch")

# Anthropic Message Batches bills token usage at 50% of standard prices.
BATCH_DISCOUNT = 0.5

_CALLED_FOR = {
    "classify": "classify_listing_images",
    "compare": "compare_listings_visually",
    "site_plan": "compare_listing_site_plans",
    "floor_plan": "compare_listing_floor_plans",
}


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

    # Poll each batch with the provider it was submitted to (dedup_batches.provider).
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
        "SELECT id, provider_batch_id, provider FROM dedup_batches "
        "WHERE status IN ('submitted', 'ended') "
        "ORDER BY submitted_at ASC LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (limit,))
        return [
            {"id": int(r[0]), "provider_batch_id": r[1], "provider": r[2] or "anthropic"}
            for r in cur.fetchall()
        ]


def _process_batch(conn: Any, providers: dict[str, Any], llm_client: Any, batch: dict[str, Any]) -> int:
    """Returns the number of cache rows persisted from this batch."""
    from api.providers import compute_cost_usd

    batch_id = batch["id"]
    provider_batch_id = batch["provider_batch_id"]
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
        _finalize_batch(conn, batch_id, done=0, errored=0, cost=0.0)
        return 0

    done = 0
    errored = 0
    cost_total = 0.0
    for item in provider.iter_batch_results(provider_batch_id):
        req = mapping.get(item.custom_id)
        if req is None:
            continue  # already ingested or unknown custom_id

        if item.status != "succeeded" or item.completion is None:
            errored += 1
            _mark_request(conn, req["id"], "errored", (item.error or item.status)[:500])
            continue

        model = req["model"]
        price = provider.price_for(model)
        cost = round(
            compute_cost_usd(price=price, model=model, usage=item.completion.usage)
            * BATCH_DISCOUNT,
            6,
        )
        outcome, spent = _ingest_one(
            conn, llm_client, req, completion=item.completion, model=model, cost=cost,
            provider=pname,
        )
        cost_total += spent
        if outcome == "done":
            done += 1
        else:
            errored += 1

    _finalize_batch(conn, batch_id, done=done, errored=errored, cost=cost_total)
    LOG.info(
        "INGEST batch_id=%d done persisted=%d errored=%d cost=$%.4f",
        batch_id, done, errored, cost_total,
    )
    return done


def _ingest_one(
    conn: Any,
    llm_client: Any,
    req: dict[str, Any],
    *,
    completion: Any,
    model: str,
    cost: float,
    provider: str = "anthropic",
) -> tuple[str, float]:
    """Record the (paid) llm_calls row and persist one result to its cache.

    The batch executed server-side, so the cost is real regardless of parse
    success — it's recorded first; a malformed tool call then marks the request
    errored (no cache row) but the audited cost stands."""
    from toolkit.image_classification import ClassifyError, persist_room_classifications
    from toolkit.visual_match import (
        VisualMatchError,
        persist_floor_plan_match,
        persist_site_plan_match,
        persist_visual_match,
    )

    kind = req["kind"]
    tool_calls = [
        {"id": tc.id, "name": tc.name, "input": tc.input}
        for tc in completion.tool_calls
    ]
    llm_call_id = llm_client.record_external_call(
        called_for=_CALLED_FOR[kind],
        provider=provider,
        model=model,
        usage=completion.usage,
        cost_usd=cost,
    )
    try:
        if kind == "classify":
            persist_room_classifications(
                conn, image_ids=req["image_ids"] or [], tool_calls=tool_calls,
                model=model, llm_call_id=llm_call_id, cost_usd=cost,
            )
        elif kind == "compare":
            persist_visual_match(
                conn, sreality_id_a=req["sreality_id_a"], sreality_id_b=req["sreality_id_b"],
                room_type=req["room_type"], tool_calls=tool_calls,
                model=model, llm_call_id=llm_call_id, cost_usd=cost,
            )
        elif kind == "site_plan":
            persist_site_plan_match(
                conn, sreality_id_a=req["sreality_id_a"], sreality_id_b=req["sreality_id_b"],
                tool_calls=tool_calls, model=model, llm_call_id=llm_call_id, cost_usd=cost,
            )
        elif kind == "floor_plan":
            persist_floor_plan_match(
                conn, sreality_id_a=req["sreality_id_a"], sreality_id_b=req["sreality_id_b"],
                tool_calls=tool_calls, model=model, llm_call_id=llm_call_id, cost_usd=cost,
            )
        else:
            raise VisualMatchError(f"unknown request kind: {kind!r}")
    except (ClassifyError, VisualMatchError) as exc:
        _mark_request(conn, req["id"], "errored", str(exc)[:500])
        return ("errored", cost)

    _mark_request(conn, req["id"], "done", None)
    return ("done", cost)


def _pending_requests(conn: Any, batch_id: int) -> dict[str, dict[str, Any]]:
    sql = (
        "SELECT id, custom_id, kind, model, sreality_id_a, sreality_id_b, room_type, image_ids "
        "FROM dedup_batch_requests WHERE batch_id = %s AND status = 'pending'"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (batch_id,))
        return {
            r[1]: {
                "id": int(r[0]), "kind": r[2], "model": r[3],
                "sreality_id_a": int(r[4]) if r[4] is not None else None,
                "sreality_id_b": int(r[5]) if r[5] is not None else None,
                "room_type": r[6],
                "image_ids": list(r[7]) if r[7] is not None else None,
            }
            for r in cur.fetchall()
        }


def _mark_request(conn: Any, request_id: int, status: str, error: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE dedup_batch_requests SET status = %s, error = %s WHERE id = %s",
            (status, error, request_id),
        )


def _update_batch_counts(conn: Any, batch_id: int, status: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE dedup_batches SET succeeded_count = %s, errored_count = %s WHERE id = %s",
            (status.counts.get("succeeded"), status.counts.get("errored"), batch_id),
        )


def _mark_batch_ended(conn: Any, batch_id: int, status: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE dedup_batches "
            "SET status = 'ended', ended_at = COALESCE(ended_at, now()), "
            "    succeeded_count = %s, errored_count = %s WHERE id = %s",
            (status.counts.get("succeeded"), status.counts.get("errored"), batch_id),
        )


def _finalize_batch(conn: Any, batch_id: int, *, done: int, errored: int, cost: float) -> None:
    """Mark a batch ingested once no pending requests remain."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM dedup_batch_requests "
            "WHERE batch_id = %s AND status = 'pending'",
            (batch_id,),
        )
        remaining = int(cur.fetchone()[0])
        new_status = "ingested" if remaining == 0 else "ended"
        cur.execute(
            "UPDATE dedup_batches "
            "SET status = %s, "
            "    ingested_count = COALESCE(ingested_count, 0) + %s, "
            "    ingest_error_count = COALESCE(ingest_error_count, 0) + %s, "
            "    total_cost_usd = COALESCE(total_cost_usd, 0) + %s, "
            "    ingested_at = CASE WHEN %s = 'ingested' THEN now() ELSE ingested_at END "
            "WHERE id = %s",
            (new_status, done, errored, cost, new_status, batch_id),
        )


if __name__ == "__main__":
    sys.exit(main())
