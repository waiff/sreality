"""compare_listings_visually: forensic same-property verdict for one room pair.

The dedup engine's visual layer (rule D) pairs LIKE rooms across two listings
(kitchen↔kitchen, bath↔bath, …) and asks the model whether the two photos
depict the same physical property. This runs that comparison for ONE room type
— a small set of images of that room from each listing — and returns a
High|Medium|Low verdict + rationale, using the operator's forensic prompt.

The engine calls this per room type in priority order and stops at the first
High (operator decision: only High auto-merges). Result cached per
(canonical pair, room_type, model) in listing_visual_matches — a re-run is free.

Write-allowed toolkit exception (CLAUDE.md toolkit rule #5). Image bytes from R2
via boto3 (scraper.image_storage), base64 into the vision payload.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

from scraper import image_storage

if TYPE_CHECKING:
    import psycopg

    from api.llm_client import LLMClient

_PROMPT_KEY = "llm_visual_match_prompt"
_MODEL_KEY = "llm_visual_match_model"
_CALLED_FOR = "compare_listings_visually"


class VisualMatchError(RuntimeError):
    """Raised when a verdict cannot be produced (no images, R2 missing, LLM refused)."""


RECORD_VISUAL_MATCH_TOOL: dict[str, Any] = {
    "name": "record_visual_match",
    "description": (
        "Record the forensic same-property verdict for the two sets of images. "
        "Call exactly once."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["High", "Medium", "Low"],
                "description": "Confidence that both image sets depict the SAME property.",
            },
            "rationale": {
                "type": "string",
                "description": "1-3 sentences: the definitive proof or red flags behind the verdict.",
            },
        },
        "required": ["verdict", "rationale"],
    },
}


def compare_listings_visually(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    sreality_id_a: int,
    sreality_id_b: int,
    room_type: str,
    image_ids_a: list[int],
    image_ids_b: list[int],
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Forensic verdict for one room type across two listings (cache on miss).

    image_ids_a / image_ids_b are the classifier-selected images of `room_type`
    for each listing (the caller picks them; this keeps the tool free of the
    classification dependency and trivially testable). data.verdict ∈
    High|Medium|Low.
    """
    from toolkit import _now_iso

    if sreality_id_a == sreality_id_b:
        raise VisualMatchError("cannot compare a listing to itself")
    a, b = sorted((sreality_id_a, sreality_id_b))
    model = llm_client.resolve_model(_MODEL_KEY)

    if not force_refresh:
        cached = _cache_lookup(conn, a, b, room_type, model)
        if cached is not None:
            return _envelope(cached, a, b, room_type, model, cache_hit=True, queried_at=_now_iso())

    verdict, rationale, cost_usd, llm_call_id = _produce(
        conn, llm_client, a, b, room_type,
        image_ids_a if a == sreality_id_a else image_ids_b,
        image_ids_b if a == sreality_id_a else image_ids_a,
        model,
    )
    _cache_store(conn, a, b, room_type, verdict, rationale, model, llm_call_id, cost_usd)
    return _envelope(
        {"verdict": verdict, "rationale": rationale, "cost_usd": cost_usd},
        a, b, room_type, model, cache_hit=False, queried_at=_now_iso(),
    )


def _produce(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    a: int,
    b: int,
    room_type: str,
    keys_a_ids: list[int],
    keys_b_ids: list[int],
    model: str,
) -> tuple[str, str, float, int]:
    if not image_storage.is_configured():
        raise VisualMatchError("R2 is not configured; cannot fetch image bytes for vision")

    keys_a = _storage_paths(conn, keys_a_ids)
    keys_b = _storage_paths(conn, keys_b_ids)
    if not keys_a or not keys_b:
        raise VisualMatchError(f"missing {room_type} images for one side ({a} or {b})")

    r2 = image_storage.R2Client.from_env()
    content: list[dict[str, Any]] = [
        {"type": "text", "text": f"Listing A — {room_type} ({len(keys_a)} image(s)):"}
    ]
    content.extend(_blocks(r2, keys_a))
    content.append({"type": "text", "text": f"Listing B — {room_type} ({len(keys_b)} image(s)):"})
    content.extend(_blocks(r2, keys_b))
    content.append({
        "type": "text",
        "text": (
            "Both sets show the same room type. Decide whether they depict the "
            "same physical property, then call record_visual_match once."
        ),
    })

    system = llm_client.resolve_system_prompt(_PROMPT_KEY)
    response = llm_client.call(
        called_for=_CALLED_FOR,
        messages=[{"role": "user", "content": content}],
        system=system,
        tools=[RECORD_VISUAL_MATCH_TOOL],
        model=model,
    )
    verdict, rationale = _extract(response.tool_calls)
    return verdict, rationale, float(response.cost_usd or 0.0), response.llm_call_id


def _storage_paths(conn: "psycopg.Connection", image_ids: list[int]) -> list[str]:
    if not image_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT storage_path FROM images "
            "WHERE id = ANY(%s) AND storage_path IS NOT NULL "
            "ORDER BY sequence ASC NULLS LAST, id ASC",
            (image_ids,),
        )
        return [r[0] for r in cur.fetchall()]


def _blocks(r2: Any, keys: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in keys:
        data = r2.download_bytes(key)
        out.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(data).decode("ascii"),
            },
        })
    return out


def _extract(tool_calls: list[dict[str, Any]]) -> tuple[str, str]:
    matching = [tc for tc in tool_calls if tc.get("name") == "record_visual_match"]
    if not matching:
        raise VisualMatchError("LLM did not invoke record_visual_match; refusing to guess")
    payload = matching[0].get("input") or {}
    verdict = payload.get("verdict")
    if verdict not in ("High", "Medium", "Low"):
        raise VisualMatchError(f"record_visual_match returned bad verdict: {verdict!r}")
    rationale = payload.get("rationale")
    return verdict, rationale if isinstance(rationale, str) else ""


def _cache_lookup(
    conn: "psycopg.Connection", a: int, b: int, room_type: str, model: str,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT verdict, rationale, cost_usd FROM listing_visual_matches "
            "WHERE sreality_id_a = %s AND sreality_id_b = %s "
            "AND room_type = %s AND model = %s",
            (a, b, room_type, model),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"verdict": row[0], "rationale": row[1], "cost_usd": float(row[2]) if row[2] is not None else None}


def _cache_store(
    conn: "psycopg.Connection",
    a: int, b: int, room_type: str, verdict: str, rationale: str,
    model: str, llm_call_id: int, cost_usd: float,
) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO listing_visual_matches "
            "(sreality_id_a, sreality_id_b, room_type, verdict, rationale, "
            " model, llm_call_id, cost_usd) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (sreality_id_a, sreality_id_b, room_type, model) DO UPDATE SET "
            "  verdict = EXCLUDED.verdict, rationale = EXCLUDED.rationale, "
            "  llm_call_id = EXCLUDED.llm_call_id, cost_usd = EXCLUDED.cost_usd, "
            "  created_at = now()",
            (a, b, room_type, verdict, rationale, model, llm_call_id, cost_usd),
        )


def _envelope(
    payload: dict[str, Any], a: int, b: int, room_type: str, model: str,
    *, cache_hit: bool, queried_at: str,
) -> dict[str, Any]:
    return {
        "data": {
            "sreality_id_a": a,
            "sreality_id_b": b,
            "room_type": room_type,
            "verdict": payload["verdict"],
            "rationale": payload.get("rationale"),
            "model": model,
            "cost_usd": payload.get("cost_usd"),
            "cache_hit": cache_hit,
        },
        "metadata": {
            "tool": "compare_listings_visually",
            "filters_used": {"sreality_id_a": a, "sreality_id_b": b, "room_type": room_type},
            "result_count": 1,
            "queried_at": queried_at,
            "data_freshness": None,
        },
    }
