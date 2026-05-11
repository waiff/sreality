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


RECORD_LISTING_SUMMARY_TOOL: dict[str, Any] = {
    "name": "record_listing_summary",
    "description": (
        "Record the structured summary for a single listing. Call "
        "exactly once with all five fields. Strict facts only; do "
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
        },
        "required": [
            "headline", "key_highlights", "concerns",
            "condition_assessment", "target_audience",
        ],
    },
}


def summarize_listing(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    sreality_id: int,
    snapshot_id: int | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    from toolkit import _now_iso

    snapshot = _resolve_snapshot(conn, sreality_id, snapshot_id)
    if snapshot is None:
        raise SummarizeError(
            f"no snapshot found for sreality_id={sreality_id}"
            + (f", snapshot_id={snapshot_id}" if snapshot_id is not None else "")
        )

    resolved_snapshot_id = snapshot["id"]
    cache_hit = False
    if not force_refresh:
        cached = _cache_lookup(conn, sreality_id, resolved_snapshot_id)
        if cached is not None:
            cache_hit = True
            summary = cached["summary"]
            model = cached["model"]
            cost_usd = cached["cost_usd"]
        else:
            summary, model, cost_usd = _produce_summary(
                conn, llm_client, sreality_id, snapshot,
            )
    else:
        summary, model, cost_usd = _produce_summary(
            conn, llm_client, sreality_id, snapshot,
        )

    return {
        "data": {
            "sreality_id": sreality_id,
            "snapshot_id": resolved_snapshot_id,
            "summary": summary,
            "model": model,
            "cost_usd": float(cost_usd) if cost_usd is not None else None,
            "cache_hit": cache_hit,
        },
        "metadata": {
            "tool": "summarize_listing",
            "filters_used": {
                "sreality_id": sreality_id,
                "snapshot_id": snapshot_id,
                "force_refresh": force_refresh,
            },
            "result_count": 1,
            "queried_at": _now_iso(),
            "data_freshness": snapshot["scraped_at"].isoformat(),
        },
    }


def _produce_summary(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    sreality_id: int,
    snapshot: dict[str, Any],
) -> tuple[dict[str, Any], str, float | None]:
    listing = _fetch_listing(conn, sreality_id)
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
        snapshot_id=snapshot["id"],
        summary=summary,
        model=response.model,
        llm_call_id=response.llm_call_id,
        cost_usd=response.cost_usd,
    )
    return summary, response.model, response.cost_usd


def _resolve_snapshot(
    conn: "psycopg.Connection",
    sreality_id: int,
    snapshot_id: int | None,
) -> dict[str, Any] | None:
    if snapshot_id is not None:
        sql = (
            "SELECT id, scraped_at, raw_json FROM listing_snapshots "
            "WHERE id = %s AND sreality_id = %s"
        )
        params: tuple[Any, ...] = (snapshot_id, sreality_id)
    else:
        sql = (
            "SELECT id, scraped_at, raw_json FROM listing_snapshots "
            "WHERE sreality_id = %s ORDER BY scraped_at DESC LIMIT 1"
        )
        params = (sreality_id,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "scraped_at": row[1], "raw_json": row[2]}


def _fetch_listing(
    conn: "psycopg.Connection",
    sreality_id: int,
) -> dict[str, Any]:
    sql = (
        "SELECT category_main, category_type, price_czk, price_unit, "
        "area_m2, disposition, locality, district, floor, "
        "has_balcony, has_parking, has_lift, "
        "building_type, condition, energy_rating "
        "FROM listings WHERE sreality_id = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (sreality_id,))
        row = cur.fetchone()
    if row is None:
        raise SummarizeError(
            f"listing sreality_id={sreality_id} has snapshot but no listings row"
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
    for key in ("headline", "key_highlights", "concerns",
                "condition_assessment", "target_audience"):
        if key not in payload:
            raise SummarizeError(f"record_listing_summary missing field: {key}")
    return dict(payload)


def _cache_lookup(
    conn: "psycopg.Connection",
    sreality_id: int,
    snapshot_id: int,
) -> dict[str, Any] | None:
    sql = (
        "SELECT summary, model, cost_usd FROM listing_summaries "
        "WHERE sreality_id = %s AND snapshot_id = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (sreality_id, snapshot_id))
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "summary": row[0],
        "model": row[1],
        "cost_usd": float(row[2]) if row[2] is not None else None,
    }


def _cache_store(
    conn: "psycopg.Connection",
    *,
    sreality_id: int,
    snapshot_id: int,
    summary: dict[str, Any],
    model: str,
    llm_call_id: int,
    cost_usd: float,
) -> None:
    sql = (
        "INSERT INTO listing_summaries "
        "(sreality_id, snapshot_id, summary, model, llm_call_id, cost_usd) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (sreality_id, snapshot_id) DO UPDATE SET "
        " summary = EXCLUDED.summary, "
        " model = EXCLUDED.model, "
        " llm_call_id = EXCLUDED.llm_call_id, "
        " cost_usd = EXCLUDED.cost_usd, "
        " created_at = now()"
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            sql,
            (
                sreality_id, snapshot_id, _Jsonb(summary),
                model, llm_call_id, cost_usd,
            ),
        )
