"""summarize_listing: structured Claude summary of a single listing snapshot.

Phase 6 visual layer. The Phase 7 reasoning agent uses this to skim
cohorts cheaply when numeric filters return many candidates — a
short headline + key highlights + concerns + a coarse condition read
is enough to triage which comparables are worth a vision pass.

Cache lives in `listing_summaries`, keyed on (sreality_id, snapshot_id).
A new snapshot (recorded only when content actually changed — see
CLAUDE.md rule #2) gets a fresh cache entry automatically.

Write-allowed exception per CLAUDE.md toolkit rule #5: same rationale
as `find_anchor_amenities` — the LLM is the source of truth, we cache
locally to keep repeat lookups fast and Anthropic-friendly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

try:
    from psycopg.types.json import Jsonb as _Jsonb
except ImportError:
    def _Jsonb(value: Any) -> Any:  # type: ignore[misc]
        return value

if TYPE_CHECKING:
    import psycopg

    from api.llm_client import LLMClient


_SYSTEM_PROMPT_KEY = "llm_summary_system_prompt"
_MODEL_KEY = "llm_summary_model"
_CALLED_FOR = "summarize_listing"

_CONDITION_VALUES = ("excellent", "good", "average", "poor", "unknown")
_AUDIENCE_VALUES = (
    "family", "couple", "single_professional",
    "investor", "student", "general",
)


class SummarizeError(RuntimeError):
    """Raised when a summary cannot be produced (no listing, no snapshot, LLM refused)."""


_REQUIRED_SUMMARY_FIELDS: tuple[str, ...] = (
    "headline", "key_highlights", "concerns",
    "condition_assessment", "target_audience",
    "location_summary", "building_summary", "apartment_summary",
)


RECORD_LISTING_SUMMARY_TOOL: dict[str, Any] = {
    "name": "record_listing_summary",
    "description": (
        "Record the structured summary for a single listing. Call "
        "exactly once with all eight fields. Strict facts only; do "
        "not invent qualities."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "headline": {
                "type": "string",
                "description": "One short sentence (max 120 chars) capturing the listing's identity.",
            },
            "key_highlights": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 0,
                "maxItems": 5,
                "description": "2-5 short strings naming attractive features stated in the input.",
            },
            "concerns": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 0,
                "maxItems": 5,
                "description": "0-5 short strings naming factual drawbacks evident in the input.",
            },
            "condition_assessment": {
                "type": "string",
                "enum": list(_CONDITION_VALUES),
                "description": "Coarse condition read derived from the listing's stated condition + description tone.",
            },
            "target_audience": {
                "type": "string",
                "enum": list(_AUDIENCE_VALUES),
                "description": "Most likely tenant fit based on size, layout, and locality cues.",
            },
            "location_summary": {
                "type": "string",
                "description": "1-2 sentences (max 240 chars) about the listing's location, neighbourhood, and transit.",
            },
            "building_summary": {
                "type": "string",
                "description": "1-2 sentences (max 240 chars) about the building: material, era, lift, common areas, energy rating.",
            },
            "apartment_summary": {
                "type": "string",
                "description": "1-2 sentences (max 240 chars) about the apartment: disposition, area, layout, condition, balcony/parking/furnishing.",
            },
        },
        "required": list(_REQUIRED_SUMMARY_FIELDS),
    },
}


def summarize_listing(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    sreality_id: int | None = None,
    snapshot_id: int | None = None,
    force_refresh: bool = False,
    listing_id: int | None = None,
) -> dict[str, Any]:
    from toolkit import _now_iso

    snapshot = _resolve_snapshot(
        conn, sreality_id, snapshot_id, listing_id=listing_id,
    )
    if snapshot is None:
        _ref = f"listing_id={listing_id}" if listing_id is not None else f"sreality_id={sreality_id}"
        raise SummarizeError(
            f"no snapshot found for {_ref}"
            + (f", snapshot_id={snapshot_id}" if snapshot_id is not None else "")
        )

    resolved_snapshot_id = snapshot["id"]
    cache_hit = False
    if not force_refresh:
        cached = _cache_lookup(
            conn, sreality_id, resolved_snapshot_id, listing_id=listing_id,
        )
        if cached is not None:
            cache_hit = True
            summary = cached["summary"]
            model = cached["model"]
            cost_usd = cached["cost_usd"]
        else:
            summary, model, cost_usd = _produce_summary(
                conn, llm_client, sreality_id, snapshot, listing_id=listing_id,
            )
    else:
        summary, model, cost_usd = _produce_summary(
            conn, llm_client, sreality_id, snapshot, listing_id=listing_id,
        )

    data: dict[str, Any] = {
        "sreality_id": sreality_id,
        "snapshot_id": resolved_snapshot_id,
        "summary": summary,
        "model": model,
        "cost_usd": float(cost_usd) if cost_usd is not None else None,
        "cache_hit": cache_hit,
    }
    filters_used: dict[str, Any] = {
        "sreality_id": sreality_id,
        "snapshot_id": snapshot_id,
        "force_refresh": force_refresh,
    }
    # Echo the surrogate handle only when addressed by it, so the sreality_id
    # path stays byte-identical.
    if listing_id is not None:
        data["listing_id"] = listing_id
        filters_used["listing_id"] = listing_id

    return {
        "data": data,
        "metadata": {
            "tool": "summarize_listing",
            "filters_used": filters_used,
            "result_count": 1,
            "queried_at": _now_iso(),
            "data_freshness": snapshot["scraped_at"].isoformat(),
        },
    }


def _produce_summary(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    sreality_id: int | None,
    snapshot: dict[str, Any],
    *,
    listing_id: int | None = None,
) -> tuple[dict[str, Any], str, float | None]:
    listing = _fetch_listing(conn, sreality_id, listing_id=listing_id)
    payload = _build_payload(listing, snapshot)

    system = llm_client.resolve_system_prompt(_SYSTEM_PROMPT_KEY)
    model = llm_client.resolve_model(_MODEL_KEY)

    response = llm_client.call(
        called_for=_CALLED_FOR,
        messages=[{"role": "user", "content": payload}],
        system=system,
        tools=[RECORD_LISTING_SUMMARY_TOOL],
        model=model,
    )
    summary = _extract_tool_call(response.tool_calls)

    _cache_store(
        conn,
        sreality_id=sreality_id,
        listing_id=listing_id,
        snapshot_id=snapshot["id"],
        summary=summary,
        model=response.model,
        llm_call_id=response.llm_call_id,
        cost_usd=response.cost_usd,
    )
    return summary, response.model, response.cost_usd


def _resolve_snapshot(
    conn: "psycopg.Connection",
    sreality_id: int | None,
    snapshot_id: int | None,
    *,
    listing_id: int | None = None,
) -> dict[str, Any] | None:
    from toolkit import _listing_id_clause

    id_clause, id_val = _listing_id_clause(
        sreality_id, listing_id, lid_col="listing_id",
    )
    if snapshot_id is not None:
        sql = (
            "SELECT id, scraped_at, raw_json FROM listing_snapshots "
            f"WHERE id = %s AND {id_clause}"
        )
        params: tuple[Any, ...] = (snapshot_id, id_val)
    else:
        sql = (
            "SELECT id, scraped_at, raw_json FROM listing_snapshots "
            f"WHERE {id_clause} ORDER BY scraped_at DESC LIMIT 1"
        )
        params = (id_val,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "scraped_at": row[1], "raw_json": row[2]}


def _fetch_listing(
    conn: "psycopg.Connection",
    sreality_id: int | None,
    *,
    listing_id: int | None = None,
) -> dict[str, Any]:
    from toolkit import _listing_id_clause

    id_clause, id_val = _listing_id_clause(sreality_id, listing_id)
    sql = (
        "SELECT category_main, category_type, price_czk, price_unit, "
        "area_m2, disposition, locality, district, floor, "
        "has_balcony, has_parking, has_lift, "
        "building_type, condition, energy_rating "
        f"FROM listings WHERE {id_clause}"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (id_val,))
        row = cur.fetchone()
    if row is None:
        _ref = f"listing_id={listing_id}" if listing_id is not None else f"sreality_id={sreality_id}"
        raise SummarizeError(
            f"listing {_ref} has snapshot but no listings row"
        )
    return {
        "category_main": row[0],
        "category_type": row[1],
        "price_czk": row[2],
        "price_unit": row[3],
        "area_m2": float(row[4]) if row[4] is not None else None,
        "disposition": row[5],
        "locality": row[6],
        "district": row[7],
        "floor": row[8],
        "has_balcony": row[9],
        "has_parking": row[10],
        "has_lift": row[11],
        "building_type": row[12],
        "condition": row[13],
        "energy_rating": row[14],
    }


def _build_payload(
    listing: dict[str, Any], snapshot: dict[str, Any],
) -> str:
    """Compact JSON-ish text payload for the LLM. Strips noisy fields from raw_json."""
    raw = snapshot.get("raw_json") or {}
    description = raw.get("text") or ""
    items = raw.get("items") or []
    items_text = "\n".join(
        f"- {it.get('name')}: {it.get('value')}"
        for it in items
        if isinstance(it, dict) and it.get("name")
    )
    structured = "\n".join(
        f"- {k}: {v}"
        for k, v in listing.items()
        if v is not None
    )
    return (
        "Structured fields:\n"
        f"{structured}\n\n"
        "Free-text description:\n"
        f"{description}\n\n"
        "Other items from the listing page:\n"
        f"{items_text}\n"
    )


def _extract_tool_call(
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    matching = [
        tc for tc in tool_calls
        if tc.get("name") == "record_listing_summary"
    ]
    if not matching:
        raise SummarizeError(
            "LLM did not invoke record_listing_summary; refusing to guess"
        )
    if len(matching) > 1:
        raise SummarizeError(
            "LLM invoked record_listing_summary more than once"
        )
    payload = matching[0].get("input") or {}
    if not isinstance(payload, dict):
        raise SummarizeError("record_listing_summary input was not an object")
    for key in _REQUIRED_SUMMARY_FIELDS:
        if key not in payload:
            raise SummarizeError(f"record_listing_summary missing field: {key}")
    return dict(payload)


def _cache_lookup(
    conn: "psycopg.Connection",
    sreality_id: int | None,
    snapshot_id: int,
    *,
    listing_id: int | None = None,
) -> dict[str, Any] | None:
    from toolkit import _listing_id_clause

    id_clause, id_val = _listing_id_clause(
        sreality_id, listing_id, lid_col="listing_id",
    )
    sql = (
        "SELECT summary, model, cost_usd FROM listing_summaries "
        f"WHERE {id_clause} AND snapshot_id = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (id_val, snapshot_id))
        row = cur.fetchone()
    if row is None:
        return None
    summary = row[0]
    if not isinstance(summary, dict) or not all(
        k in summary for k in _REQUIRED_SUMMARY_FIELDS
    ):
        return None
    return {
        "summary": summary,
        "model": row[1],
        "cost_usd": float(row[2]) if row[2] is not None else None,
    }


def _cache_store(
    conn: "psycopg.Connection",
    *,
    sreality_id: int | None,
    snapshot_id: int,
    summary: dict[str, Any],
    model: str,
    llm_call_id: int,
    cost_usd: float,
    listing_id: int | None = None,
) -> None:
    # Arbiter is listing_id (R2 Phase C, listing_summaries_listing_id_snapshot_id_key).
    # We supply the id we were addressed by and derive the sibling column from
    # listings — from the surrogate when named by listing_id (the sreality_id
    # column then reflects the row, NULL post-Gate-2), else from the legacy
    # handle (the dual-write phase of migrations 320-325).
    if listing_id is not None:
        id_values = "(SELECT sreality_id FROM listings WHERE id = %(listing_id)s), %(listing_id)s"
    else:
        id_values = "%(sreality_id)s, (SELECT id FROM listings WHERE sreality_id = %(sreality_id)s)"
    sql = (
        "INSERT INTO listing_summaries "
        "(sreality_id, listing_id, snapshot_id, summary, model, llm_call_id, cost_usd) "
        f"VALUES ({id_values}, %(snapshot_id)s, %(summary)s, %(model)s, %(llm_call_id)s, %(cost_usd)s) "
        "ON CONFLICT (listing_id, snapshot_id) DO UPDATE SET "
        " listing_id = EXCLUDED.listing_id, "
        " summary = EXCLUDED.summary, "
        " model = EXCLUDED.model, "
        " llm_call_id = EXCLUDED.llm_call_id, "
        " cost_usd = EXCLUDED.cost_usd, "
        " created_at = now()"
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            sql,
            {
                "sreality_id": sreality_id,
                "listing_id": listing_id,
                "snapshot_id": snapshot_id,
                "summary": _Jsonb(summary),
                "model": model,
                "llm_call_id": llm_call_id,
                "cost_usd": cost_usd,
            },
        )
