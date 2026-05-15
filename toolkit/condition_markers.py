"""discover_condition_markers: one-off LLM-driven mining of Czech condition markers.

Phase A of the building/apartment condition-scoring feature. Each call
sends one listing's structured fields + free-text description + items[]
+ the first N images to Claude. The LLM extracts every condition
marker it can spot, splits them into building-scoped vs apartment-
scoped, and tags each with sentiment + suggested level implication.

Cache lives in `listing_marker_extractions`, keyed on
(sreality_id, snapshot_id). Auto-invalidates when a new snapshot is
recorded — same pattern as `listing_summaries` (migration 027).

Write-allowed exception per CLAUDE.md toolkit rule #5: same rationale
as `summarize_listing` and `compare_listing_images` — the LLM is the
source of truth for the extraction, we cache locally so the operator
can iterate on the aggregator + rubric without re-billing per pass.

This function is the per-listing primitive. The fleet-level discovery
loop lives in `scripts/discover_condition_markers.py`, and the
post-hoc deduplication / clustering lives in
`scripts/aggregate_condition_markers.py`.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

from scraper import image_storage

try:
    from psycopg.types.json import Jsonb as _Jsonb
except ImportError:
    def _Jsonb(value: Any) -> Any:  # type: ignore[misc]
        return value

if TYPE_CHECKING:
    import psycopg

    from api.llm_client import LLMClient


_SYSTEM_PROMPT_KEY = "llm_condition_discovery_system_prompt"
_MODEL_KEY = "llm_condition_discovery_model"
_CALLED_FOR = "discover_condition_markers"

_SCOPE_VALUES = ("building", "apartment")
_SENTIMENT_VALUES = ("positive", "negative", "neutral")
_LEVEL_HINT_VALUES = ("high", "medium", "low")
_SOURCE_VALUES = ("text", "items", "image")

_MAX_MARKERS_PER_LISTING = 30


class DiscoveryError(RuntimeError):
    """Raised when a discovery extraction cannot be produced."""


RECORD_LISTING_MARKERS_TOOL: dict[str, Any] = {
    "name": "record_listing_markers",
    "description": (
        "Record the structured list of Czech condition markers found in "
        "one listing. Call exactly once. Building-scoped and apartment-"
        "scoped markers go in the same flat list, distinguished by the "
        "`scope` field on each entry."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "markers": {
                "type": "array",
                "minItems": 0,
                "maxItems": _MAX_MARKERS_PER_LISTING,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "marker_text": {
                            "type": "string",
                            "description": (
                                "Canonical Czech phrase, lowercase, "
                                "2-5 words preferred."
                            ),
                        },
                        "scope": {
                            "type": "string",
                            "enum": list(_SCOPE_VALUES),
                        },
                        "evidence_quote": {
                            "type": "string",
                            "description": (
                                "Exact substring of description / item, "
                                "or 'image:N' for image-only markers. "
                                "Max 200 chars."
                            ),
                        },
                        "sentiment": {
                            "type": "string",
                            "enum": list(_SENTIMENT_VALUES),
                        },
                        "suggested_level_implication": {
                            "type": "string",
                            "enum": list(_LEVEL_HINT_VALUES),
                        },
                        "source": {
                            "type": "string",
                            "enum": list(_SOURCE_VALUES),
                        },
                    },
                    "required": [
                        "marker_text", "scope", "evidence_quote",
                        "sentiment", "suggested_level_implication",
                        "source",
                    ],
                },
            },
            "notes": {
                "type": "string",
                "description": (
                    "Free-form 0-400 chars: ambiguities the operator "
                    "should know about. Empty string if none."
                ),
            },
        },
        "required": ["markers", "notes"],
    },
}


def discover_condition_markers(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    sreality_id: int,
    snapshot_id: int | None = None,
    n_images: int = 5,
    force_refresh: bool = False,
) -> dict[str, Any]:
    from toolkit import _now_iso

    snapshot = _resolve_snapshot(conn, sreality_id, snapshot_id)
    if snapshot is None:
        raise DiscoveryError(
            f"no snapshot found for sreality_id={sreality_id}"
            + (f", snapshot_id={snapshot_id}" if snapshot_id is not None else "")
        )

    resolved_snapshot_id = snapshot["id"]
    cache_hit = False
    if not force_refresh:
        cached = _cache_lookup(conn, sreality_id, resolved_snapshot_id)
        if cached is not None:
            cache_hit = True
            markers = cached["markers"]
            notes = cached["notes"]
            model = cached["model"]
            cost_usd = cached["cost_usd"]
            n_images_used = cached["n_images"]
        else:
            markers, notes, model, cost_usd, n_images_used = _produce_extraction(
                conn, llm_client, sreality_id, snapshot, n_images,
            )
    else:
        markers, notes, model, cost_usd, n_images_used = _produce_extraction(
            conn, llm_client, sreality_id, snapshot, n_images,
        )

    return {
        "data": {
            "sreality_id": sreality_id,
            "snapshot_id": resolved_snapshot_id,
            "markers": markers,
            "notes": notes,
            "n_images": n_images_used,
            "model": model,
            "cost_usd": float(cost_usd) if cost_usd is not None else None,
            "cache_hit": cache_hit,
        },
        "metadata": {
            "tool": "discover_condition_markers",
            "filters_used": {
                "sreality_id": sreality_id,
                "snapshot_id": snapshot_id,
                "n_images": n_images,
                "force_refresh": force_refresh,
            },
            "result_count": len(markers),
            "queried_at": _now_iso(),
            "data_freshness": snapshot["scraped_at"].isoformat(),
        },
    }


def _produce_extraction(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    sreality_id: int,
    snapshot: dict[str, Any],
    n_images: int,
) -> tuple[list[dict[str, Any]], str, str, float | None, int]:
    listing = _fetch_listing(conn, sreality_id)
    text_payload = _build_text_payload(listing, snapshot)

    image_blocks = _build_image_blocks_if_available(conn, sreality_id, n_images)

    content: list[dict[str, Any]] = [{"type": "text", "text": text_payload}]
    if image_blocks:
        content.append({
            "type": "text",
            "text": f"Listing images ({len(image_blocks)}):",
        })
        content.extend(image_blocks)
    content.append({
        "type": "text",
        "text": (
            "Extract every condition marker you can spot. Building-scoped "
            "and apartment-scoped markers go in the same flat list, "
            "distinguished by the `scope` field. Skip amenities. Empty "
            "list is acceptable if no concrete marker is present."
        ),
    })

    system = llm_client.resolve_system_prompt(_SYSTEM_PROMPT_KEY)
    model = llm_client.resolve_model(_MODEL_KEY)

    response = llm_client.call(
        called_for=_CALLED_FOR,
        messages=[{"role": "user", "content": content}],
        system=system,
        tools=[RECORD_LISTING_MARKERS_TOOL],
        model=model,
    )
    markers, notes = _extract_tool_call(response.tool_calls)

    _cache_store(
        conn,
        sreality_id=sreality_id,
        snapshot_id=snapshot["id"],
        markers=markers,
        notes=notes,
        n_images=len(image_blocks),
        model=response.model,
        llm_call_id=response.llm_call_id,
        cost_usd=response.cost_usd,
    )
    return markers, notes, response.model, response.cost_usd, len(image_blocks)


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
        "area_m2, disposition, locality, district, floor, total_floors, "
        "has_balcony, has_parking, has_lift, "
        "building_type, condition, energy_rating "
        "FROM listings WHERE sreality_id = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (sreality_id,))
        row = cur.fetchone()
    if row is None:
        raise DiscoveryError(
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
        "total_floors": row[9],
        "has_balcony": row[10],
        "has_parking": row[11],
        "has_lift": row[12],
        "building_type": row[13],
        "condition": row[14],
        "energy_rating": row[15],
    }


def _build_text_payload(
    listing: dict[str, Any], snapshot: dict[str, Any],
) -> str:
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
        "Free-text description (Czech):\n"
        f"{description}\n\n"
        "Other items from the listing page:\n"
        f"{items_text}\n"
    )


def _build_image_blocks_if_available(
    conn: "psycopg.Connection",
    sreality_id: int,
    n_images: int,
) -> list[dict[str, Any]]:
    if n_images <= 0:
        return []
    if not image_storage.is_configured():
        return []
    keys = _fetch_image_keys(conn, sreality_id, n_images)
    if not keys:
        return []
    r2 = image_storage.R2Client.from_env()
    blocks: list[dict[str, Any]] = []
    for key in keys:
        data = r2.download_bytes(key)
        encoded = base64.standard_b64encode(data).decode("ascii")
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": encoded,
            },
        })
    return blocks


def _fetch_image_keys(
    conn: "psycopg.Connection",
    sreality_id: int,
    n_images: int,
) -> list[str]:
    sql = (
        "SELECT storage_path FROM images "
        "WHERE sreality_id = %s AND storage_path IS NOT NULL "
        "ORDER BY sequence ASC NULLS LAST LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (sreality_id, n_images))
        rows = cur.fetchall()
    return [r[0] for r in rows]


def _extract_tool_call(
    tool_calls: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    matching = [
        tc for tc in tool_calls
        if tc.get("name") == "record_listing_markers"
    ]
    if not matching:
        raise DiscoveryError(
            "LLM did not invoke record_listing_markers; refusing to guess"
        )
    if len(matching) > 1:
        raise DiscoveryError(
            "LLM invoked record_listing_markers more than once"
        )
    payload = matching[0].get("input") or {}
    if not isinstance(payload, dict):
        raise DiscoveryError("record_listing_markers input was not an object")
    markers = payload.get("markers")
    if not isinstance(markers, list):
        raise DiscoveryError("record_listing_markers missing markers list")
    if len(markers) > _MAX_MARKERS_PER_LISTING:
        raise DiscoveryError(
            f"record_listing_markers returned {len(markers)} markers; "
            f"max is {_MAX_MARKERS_PER_LISTING}"
        )
    for i, m in enumerate(markers):
        _validate_marker(m, i)
    notes = payload.get("notes")
    if not isinstance(notes, str):
        raise DiscoveryError("record_listing_markers missing notes string")
    return [dict(m) for m in markers], notes


def _validate_marker(marker: Any, idx: int) -> None:
    if not isinstance(marker, dict):
        raise DiscoveryError(f"marker[{idx}] is not an object")
    for field in (
        "marker_text", "scope", "evidence_quote",
        "sentiment", "suggested_level_implication", "source",
    ):
        if field not in marker:
            raise DiscoveryError(f"marker[{idx}] missing field: {field}")
    if marker["scope"] not in _SCOPE_VALUES:
        raise DiscoveryError(
            f"marker[{idx}] scope={marker['scope']!r} not in {_SCOPE_VALUES}"
        )
    if marker["sentiment"] not in _SENTIMENT_VALUES:
        raise DiscoveryError(
            f"marker[{idx}] sentiment={marker['sentiment']!r} not in {_SENTIMENT_VALUES}"
        )
    if marker["suggested_level_implication"] not in _LEVEL_HINT_VALUES:
        raise DiscoveryError(
            f"marker[{idx}] suggested_level_implication="
            f"{marker['suggested_level_implication']!r} not in {_LEVEL_HINT_VALUES}"
        )
    if marker["source"] not in _SOURCE_VALUES:
        raise DiscoveryError(
            f"marker[{idx}] source={marker['source']!r} not in {_SOURCE_VALUES}"
        )


def _cache_lookup(
    conn: "psycopg.Connection",
    sreality_id: int,
    snapshot_id: int,
) -> dict[str, Any] | None:
    sql = (
        "SELECT markers, notes, n_images, model, cost_usd "
        "FROM listing_marker_extractions "
        "WHERE sreality_id = %s AND snapshot_id = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (sreality_id, snapshot_id))
        row = cur.fetchone()
    if row is None:
        return None
    markers = row[0]
    if not isinstance(markers, list):
        return None
    return {
        "markers": markers,
        "notes": row[1] or "",
        "n_images": row[2],
        "model": row[3],
        "cost_usd": float(row[4]) if row[4] is not None else None,
    }


def _cache_store(
    conn: "psycopg.Connection",
    *,
    sreality_id: int,
    snapshot_id: int,
    markers: list[dict[str, Any]],
    notes: str,
    n_images: int,
    model: str,
    llm_call_id: int,
    cost_usd: float,
) -> None:
    sql = (
        "INSERT INTO listing_marker_extractions "
        "(sreality_id, snapshot_id, markers, notes, n_images, "
        " model, llm_call_id, cost_usd) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (sreality_id, snapshot_id) DO UPDATE SET "
        " markers = EXCLUDED.markers, "
        " notes = EXCLUDED.notes, "
        " n_images = EXCLUDED.n_images, "
        " model = EXCLUDED.model, "
        " llm_call_id = EXCLUDED.llm_call_id, "
        " cost_usd = EXCLUDED.cost_usd, "
        " created_at = now()"
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            sql,
            (
                sreality_id, snapshot_id, _Jsonb(markers), notes,
                n_images, model, llm_call_id, cost_usd,
            ),
        )
