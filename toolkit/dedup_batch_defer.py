"""Engine-fed batch deferral (dedup-cost-reduction.md §4.1): when a sweep lane
hits a cold (cache-miss) vision call it would otherwise pay for inline, it
calls `enqueue_deferred_request` instead of the LLM. The request is built
exactly as it would be for a live call (byte-identical payload — same
`build_*_request` helpers `scripts/submit_dedup_batch.py`'s retired collect()
used), then stored ready-to-submit in `dedup_batch_requests` with `batch_id`
NULL — the "spool". `scripts/submit_dedup_batch.py` periodically flushes the
spool into provider Batch API submissions (50% off); the next lane pass finds
the verdict warm in cache and decides for free.

Because the ENGINE is the one deciding what to defer (not a second process
guessing the work-list), selection identity holds by construction: whatever
the sweep lane would have paid for synchronously is exactly what gets warmed.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg.types.json import Jsonb

LOG = logging.getLogger("dedup_batch_defer")


def enqueue_deferred_request(
    conn: Any,
    providers: dict[str, Any],
    *,
    custom_id: str,
    kind: str,
    model: str,
    sreality_id_a: int,
    sreality_id_b: int | None,
    room_type: str | None,
    build_fn: Any,
) -> bool:
    """Spool one cold vision request instead of calling the LLM live.

    Idempotent: a custom_id already `pending` (spooled-unsubmitted OR
    submitted-but-not-yet-ingested — both states are `status='pending'`) is
    left alone and this returns True without re-enqueueing, so overlapping
    lane passes never double-spool or double-bill one request. `build_fn` is
    a thunk (only invoked on an actual miss) that returns the same
    `{system, messages, tools, model, image_ids?}` dict a live call's request
    builder would.

    Returns True once the request is spooled (new or already pending), False
    when it could not be built or has no batch-capable provider for its model
    — the caller should treat False the same as a real cache-miss with no
    warming available (defer the pair anyway; it will retry on cache-miss
    again next pass, same as today's un-warmed floor-plan behaviour)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM dedup_batch_requests WHERE custom_id = %s AND status = 'pending'",
            (custom_id,),
        )
        if cur.fetchone() is not None:
            return True

    try:
        built = build_fn()
    except Exception as exc:  # noqa: BLE001 - one bad pair must not kill the run
        LOG.warning("DEFER build %s failed: %s", custom_id, exc)
        return False

    from api.llm_client import provider_for_model
    pname = provider_for_model(built["model"])
    provider = providers.get(pname)
    if provider is None:
        # A model whose provider isn't batch-wired (e.g. gemini/qwen): can't
        # defer it. The caller falls back to its own miss handling.
        LOG.warning(
            "DEFER no batch provider %r for model %s; skipped %s",
            pname, built["model"], custom_id,
        )
        return False

    params = provider.build_batch_request_params(
        system=built["system"], messages=built["messages"],
        tools=built["tools"], model=built["model"],
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO dedup_batch_requests "
            "(batch_id, custom_id, kind, model, sreality_id_a, sreality_id_b, "
            " room_type, image_ids, request_params, status) "
            "VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s, 'pending') "
            "ON CONFLICT (custom_id) WHERE batch_id IS NULL DO NOTHING",
            (custom_id, kind, model, sreality_id_a, sreality_id_b, room_type,
             built.get("image_ids"), Jsonb(params)),
        )
    LOG.debug("DEFER spooled %s kind=%s model=%s", custom_id, kind, model)
    return True
