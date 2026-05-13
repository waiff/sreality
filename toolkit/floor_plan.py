"""read_floor_plan: structured analysis of one operator-supplied attachment.

Pattern mirrors toolkit.summaries.summarize_listing and
toolkit.building_extraction.extract_building_units:

- One Claude vision call per cache miss.
- Cache keyed on (attachment_id, model) so a model bump invalidates
  without manual cleanup.
- Write-allowed exception per CLAUDE.md toolkit rule #5; the LLM is
  the source of truth for the structured analysis, we cache locally
  so the agent loop's repeat invocations and re-runs don't re-bill.

Authorisation is the caller's responsibility — the agent registry
handler in api/agent.py checks that the requested attachment_id
belongs to the current building_run before dispatching here.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Any

try:
    from psycopg.types.json import Jsonb as _Jsonb
except ImportError:
    def _Jsonb(value: Any) -> Any:  # type: ignore[misc]
        return value

if TYPE_CHECKING:
    import psycopg

    from api.llm_client import LLMClient

LOG = logging.getLogger(__name__)


_SYSTEM_PROMPT_KEY = "llm_floorplan_system_prompt"
_MODEL_KEY = "llm_floorplan_model"
_CALLED_FOR = "read_floor_plan"

_IMAGE_KIND_VALUES = (
    "floor_plan", "photo_interior", "photo_exterior",
    "technical_drawing", "other",
)
_CONFIDENCE_VALUES = ("high", "medium", "low")


class FloorPlanReadError(RuntimeError):
    """Raised when the analysis cannot be produced."""


def _room_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "label": {"type": "string", "minLength": 1, "maxLength": 80},
            "area_m2": {"type": ["number", "null"], "minimum": 0},
            "is_potential": {"type": "boolean"},
        },
        "required": ["label", "area_m2", "is_potential"],
    }


RECORD_FLOOR_PLAN_ANALYSIS_TOOL: dict[str, Any] = {
    "name": "record_floor_plan_analysis",
    "description": (
        "Record the structured analysis of one operator-supplied "
        "attachment image. Call exactly once."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "headline": {"type": "string", "minLength": 1, "maxLength": 120},
            "image_kind": {"type": "string", "enum": list(_IMAGE_KIND_VALUES)},
            "rooms": {
                "type": "array",
                "items": _room_schema(),
                "minItems": 0,
                "maxItems": 30,
            },
            "total_area_m2": {"type": ["number", "null"], "minimum": 0},
            "layout_text": {"type": "string", "minLength": 1, "maxLength": 1200},
            "confidence": {"type": "string", "enum": list(_CONFIDENCE_VALUES)},
        },
        "required": [
            "headline", "image_kind", "rooms",
            "total_area_m2", "layout_text", "confidence",
        ],
    },
}


def read_floor_plan(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    attachment_id: int,
    force_refresh: bool = False,
    estimation_run_id: int | None = None,
) -> dict[str, Any]:
    """Read one operator-supplied attachment via Claude vision.

    Returns the standard toolkit envelope. Cached per
    (attachment_id, model) in `building_attachment_analyses`; a model
    bump produces a fresh row automatically.
    """
    from api.attachments import fetch_attachment
    from toolkit import _now_iso

    attachment = fetch_attachment(conn, attachment_id)
    if attachment is None:
        raise FloorPlanReadError(f"attachment_id={attachment_id} not found")

    model = llm_client.resolve_model(_MODEL_KEY)

    cache_hit = False
    if not force_refresh:
        cached = _cache_lookup(conn, attachment_id, model)
        if cached is not None:
            cache_hit = True
            analysis = cached
        else:
            analysis = _produce(
                conn, llm_client, attachment, model, estimation_run_id,
            )
    else:
        analysis = _produce(
            conn, llm_client, attachment, model, estimation_run_id,
        )

    return {
        "data": {
            "attachment_id": attachment_id,
            "filename": attachment["filename"],
            "mime_type": attachment["mime_type"],
            **analysis,
            "cache_hit": cache_hit,
        },
        "metadata": {
            "tool": "read_floor_plan",
            "filters_used": {
                "attachment_id": attachment_id,
                "force_refresh": force_refresh,
            },
            "result_count": len(analysis.get("rooms") or []),
            "queried_at": _now_iso(),
            "data_freshness": attachment["created_at"],
        },
    }


def _produce(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    attachment: dict[str, Any],
    model: str,
    estimation_run_id: int | None,
) -> dict[str, Any]:
    from api.attachments import download_attachment_bytes

    data, mime, filename = download_attachment_bytes(conn, attachment["id"])
    encoded = base64.standard_b64encode(data).decode("ascii")

    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"Operator-supplied attachment {filename!r} "
                f"(mime={mime}). Analyse per the system prompt and "
                "call record_floor_plan_analysis exactly once."
            ),
        },
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": encoded,
            },
        },
    ]
    system = llm_client.resolve_system_prompt(_SYSTEM_PROMPT_KEY)

    response = llm_client.call(
        called_for=_CALLED_FOR,
        messages=[{"role": "user", "content": content}],
        system=system,
        tools=[RECORD_FLOOR_PLAN_ANALYSIS_TOOL],
        model=model,
        estimation_run_id=estimation_run_id,
    )
    payload = _extract_tool_call(response.tool_calls)
    analysis = _normalize(payload)

    _cache_store(
        conn,
        attachment_id=attachment["id"],
        model=response.model,
        analysis=analysis,
        llm_call_id=response.llm_call_id,
        cost_usd=response.cost_usd,
    )
    return analysis


def _extract_tool_call(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    matching = [
        tc for tc in tool_calls
        if tc.get("name") == "record_floor_plan_analysis"
    ]
    if not matching:
        raise FloorPlanReadError(
            "LLM did not invoke record_floor_plan_analysis; refusing to guess"
        )
    if len(matching) > 1:
        raise FloorPlanReadError(
            "LLM invoked record_floor_plan_analysis more than once"
        )
    payload = matching[0].get("input") or {}
    if not isinstance(payload, dict):
        raise FloorPlanReadError("record_floor_plan_analysis input was not an object")
    for key in (
        "headline", "image_kind", "rooms",
        "total_area_m2", "layout_text", "confidence",
    ):
        if key not in payload:
            raise FloorPlanReadError(
                f"record_floor_plan_analysis missing field: {key}"
            )
    return payload


def _normalize(payload: dict[str, Any]) -> dict[str, Any]:
    rooms_raw = payload.get("rooms") or []
    rooms: list[dict[str, Any]] = []
    for r in rooms_raw:
        if not isinstance(r, dict):
            continue
        rooms.append({
            "label": str(r.get("label") or ""),
            "area_m2": r.get("area_m2"),
            "is_potential": bool(r.get("is_potential", False)),
        })
    return {
        "headline": str(payload["headline"]),
        "image_kind": str(payload["image_kind"]),
        "rooms": rooms,
        "total_area_m2": payload.get("total_area_m2"),
        "layout_text": str(payload["layout_text"]),
        "confidence": str(payload["confidence"]),
    }


def _cache_lookup(
    conn: "psycopg.Connection",
    attachment_id: int,
    model: str,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT analysis FROM building_attachment_analyses "
            "WHERE attachment_id = %s AND model = %s",
            (attachment_id, model),
        )
        row = cur.fetchone()
    if row is None:
        return None
    analysis = row[0]
    if not isinstance(analysis, dict):
        return None
    return analysis


def _cache_store(
    conn: "psycopg.Connection",
    *,
    attachment_id: int,
    model: str,
    analysis: dict[str, Any],
    llm_call_id: int,
    cost_usd: float,
) -> None:
    sql = (
        "INSERT INTO building_attachment_analyses "
        "(attachment_id, model, analysis, llm_call_id, cost_usd) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (attachment_id, model) DO UPDATE SET "
        " analysis = EXCLUDED.analysis, "
        " llm_call_id = EXCLUDED.llm_call_id, "
        " cost_usd = EXCLUDED.cost_usd, "
        " created_at = now()"
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            sql,
            (attachment_id, model, _Jsonb(analysis), llm_call_id, cost_usd),
        )
