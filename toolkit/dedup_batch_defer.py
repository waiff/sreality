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

**Identity (R2 PR2).** This module owns the `custom_id` scheme so no caller can
reintroduce a legacy-keyed one. Two properties make the spool survive the
listing-identity refactor without further changes:

1. `custom_id` is derived from the SURROGATE `listings.id`, under its own
   prefixes (`clsL-`/`cmpL-`/`splL-`/`fplL-`, distinct from the legacy
   `cls-`/`cmp-`/`spl-`/`fpl-` so the two schemes can never be confused — the
   two id-spaces overlap numerically, so the prefix is the only thing that
   tells them apart).
2. For a pair it uses min/max of the two surrogates, so it is INVARIANT under
   the positional order of `(a, b)`. That matters because the columns keep the
   caller's positional order (the R2 pair-carrier convention: `listing_id_a` is
   the surrogate OF `sreality_id_a`; nothing here ever reorders a pair, because
   the request payload's image sides are ordered in lockstep with it inside
   `build_*_request`). When a later PR changes what the engine canonicalizes
   on, the columns may flip sides — and the custom_id, hence the idempotency
   key, will not.

Callers may identify a listing by either key: pass `sreality_id_*`,
`listing_id_*`, or both. Whatever is missing is resolved here, and both spaces
are written, so this keeps working before and after the PK swap.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg.types.json import Jsonb

LOG = logging.getLogger("dedup_batch_defer")

# Surrogate-keyed custom_id prefixes. Deliberately NOT the legacy
# cls-/cmp-/spl-/fpl-: a legacy and a surrogate id can be the same number, so
# only the prefix distinguishes "cmp-500-600" from "cmpL-500-600".
_CUSTOM_ID_PREFIX = {
    "classify": "clsL",
    "compare": "cmpL",
    "site_plan": "splL",
    "floor_plan": "fplL",
}

_RESOLVE_SQL = (
    "SELECT sreality_id, id FROM listings WHERE sreality_id = ANY(%(sids)s) "
    "UNION ALL "
    "SELECT sreality_id, id FROM listings WHERE id = ANY(%(lids)s)"
)


def _custom_id(
    kind: str, listing_id_a: int, listing_id_b: int | None, room_type: str | None,
) -> str:
    """The spool's idempotency key: surrogate-derived, order-independent."""
    prefix = _CUSTOM_ID_PREFIX[kind]
    if listing_id_b is None:
        return f"{prefix}-{listing_id_a}"
    lo, hi = sorted((listing_id_a, listing_id_b))
    base = f"{prefix}-{lo}-{hi}"
    return f"{base}-{room_type}" if room_type else base


def _resolve_identity(
    conn: Any, *, sreality_ids: list[int], listing_ids: list[int],
) -> tuple[dict[int, int], dict[int, int]]:
    """(surrogate by legacy id, legacy id by surrogate) for the listings named
    by either key. Two indexed arms UNIONed rather than one OR'd predicate —
    an OR across two index arms plans as a full scan on this table."""
    with conn.cursor() as cur:
        cur.execute(_RESOLVE_SQL, {"sids": sreality_ids, "lids": listing_ids})
        rows = cur.fetchall()
    by_sreality = {int(r[0]): int(r[1]) for r in rows if r[0] is not None}
    by_listing = {int(r[1]): int(r[0]) for r in rows if r[0] is not None}
    return by_sreality, by_listing


def enqueue_deferred_request(
    conn: Any,
    providers: dict[str, Any],
    *,
    kind: str,
    model: str,
    sreality_id_a: int | None = None,
    sreality_id_b: int | None = None,
    listing_id_a: int | None = None,
    listing_id_b: int | None = None,
    room_type: str | None = None,
    build_fn: Any,
) -> bool:
    """Spool one cold vision request instead of calling the LLM live.

    Identify the listing(s) by either key — the other is resolved here and both
    are stored. Side `a` and side `b` keep the caller's positional order (the
    request payload's image sides are ordered to match); only the derived
    `custom_id` is order-independent.

    Idempotent: a custom_id already `pending` (spooled-unsubmitted OR
    submitted-but-not-yet-ingested — both states are `status='pending'`) is
    left alone and this returns True without re-enqueueing, so overlapping
    lane passes never double-spool or double-bill one request. `build_fn` is
    a thunk (only invoked on an actual miss) that returns the same
    `{system, messages, tools, model, image_ids?}` dict a live call's request
    builder would.

    Returns True once the request is spooled (new or already pending), False
    when its identity cannot be resolved, it could not be built, or it has no
    batch-capable provider for its model — the caller should treat False the
    same as a real cache-miss with no warming available (defer the pair anyway;
    it will retry on cache-miss again next pass, same as today's un-warmed
    floor-plan behaviour)."""
    # Whether this is a pair is a property of what the CALLER passed, so read it
    # before resolution starts filling the other space in.
    is_pair = sreality_id_b is not None or listing_id_b is not None
    want_sids = [s for s in (sreality_id_a, sreality_id_b) if s is not None]
    want_lids = [i for i in (listing_id_a, listing_id_b) if i is not None]
    if not want_sids and not want_lids:
        LOG.warning("DEFER no identity given kind=%s; skipped", kind)
        return False

    by_sreality, by_listing = _resolve_identity(
        conn, sreality_ids=want_sids, listing_ids=want_lids)

    def _both(sid: int | None, lid: int | None) -> tuple[int | None, int | None]:
        if lid is None and sid is not None:
            lid = by_sreality.get(sid)
        if sid is None and lid is not None:
            sid = by_listing.get(lid)
        return sid, lid

    sreality_id_a, listing_id_a = _both(sreality_id_a, listing_id_a)
    sreality_id_b, listing_id_b = _both(sreality_id_b, listing_id_b)

    # The surrogate is the spool's key — an unresolvable side (listing deleted
    # between the engine's load and here) cannot be spooled at all.
    if listing_id_a is None or (is_pair and listing_id_b is None):
        LOG.warning(
            "DEFER unresolved listing identity kind=%s a=(%s,%s) b=(%s,%s); skipped",
            kind, sreality_id_a, listing_id_a, sreality_id_b, listing_id_b,
        )
        return False

    custom_id = _custom_id(kind, listing_id_a, listing_id_b, room_type)

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
            " listing_id_a, listing_id_b, room_type, image_ids, request_params, status) "
            "VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending') "
            "ON CONFLICT (custom_id) WHERE batch_id IS NULL DO NOTHING",
            (custom_id, kind, model, sreality_id_a, sreality_id_b,
             listing_id_a, listing_id_b, room_type,
             built.get("image_ids"), Jsonb(params)),
        )
    LOG.debug("DEFER spooled %s kind=%s model=%s", custom_id, kind, model)
    return True
