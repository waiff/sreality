"""classify_listing_images: per-image room-type labels via Claude vision.

The dedup engine's visual layer pairs LIKE rooms before the forensic
same-property comparison, so it first needs to know what each photo
depicts. This labels every stored image of one listing into the room
taxonomy (migration 128) in a single vision call and caches the result
per (image_id, model).

Write-allowed toolkit exception (CLAUDE.md toolkit rule #5): the LLM is
the source of truth, image_room_classifications is a local mirror that
auto-invalidates on a model bump. Image bytes come from R2 via boto3
(reusing scraper.image_storage), base64-encoded into the vision payload.
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

_PROMPT_KEY = "llm_room_classify_prompt"
_MODEL_KEY = "llm_room_classify_model"
_CALLED_FOR = "classify_listing_images"

ROOM_TYPES = (
    "kitchen", "bathroom", "toilet", "living_room", "bedroom", "hallway",
    "exterior_facade", "balcony_terrace", "garden", "floor_plan", "other",
)

# Interior types carry the strongest same-flat signal; the pHash fast-path uses
# this set to exclude facade / floor-plan shots (whole developments reuse one
# such image across distinct units).
INTERIOR_ROOM_TYPES = frozenset({
    "kitchen", "bathroom", "toilet", "living_room", "bedroom", "hallway",
})


class ClassifyError(RuntimeError):
    """Raised when classification cannot be produced (no images, R2 missing, LLM refused)."""


RECORD_ROOM_TYPES_TOOL: dict[str, Any] = {
    "name": "record_room_types",
    "description": (
        "Record the room type of each listing image, in the same order the "
        "images were presented. Call exactly once with one entry per image."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "rooms": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": "0-based position of the image as presented.",
                        },
                        "room_type": {"type": "string", "enum": list(ROOM_TYPES)},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["index", "room_type", "confidence"],
                },
            },
        },
        "required": ["rooms"],
    },
}


def classify_listing_images(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    sreality_id: int,
    n_images: int = 12,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Return per-image room types for one listing, classifying on cache miss.

    data.images is a list of {image_id, sequence, storage_path, room_type,
    confidence}; the engine uses room_type to pair like rooms and to gate the
    pHash fast-path on interior shots only.
    """
    from toolkit import _now_iso

    model = llm_client.resolve_model(_MODEL_KEY)
    images = _fetch_images(conn, sreality_id, n_images)
    if not images:
        raise ClassifyError(f"no R2-stored images for sreality_id={sreality_id}")

    cached = _cache_lookup(conn, [img["id"] for img in images], model)
    missing = [img for img in images if img["id"] not in cached]

    cost_usd = 0.0
    if missing and not force_refresh:
        produced, cost_usd = _classify_missing(conn, llm_client, missing, model)
        cached.update(produced)
    elif missing and force_refresh:
        produced, cost_usd = _classify_missing(conn, llm_client, images, model)
        cached.update(produced)

    out = []
    for img in images:
        rc = cached.get(img["id"])
        out.append({
            "image_id": img["id"],
            "sequence": img["sequence"],
            "storage_path": img["storage_path"],
            "room_type": rc["room_type"] if rc else "other",
            "confidence": rc["confidence"] if rc else "low",
        })

    return {
        "data": {"sreality_id": sreality_id, "model": model, "images": out},
        "metadata": {
            "tool": "classify_listing_images",
            "filters_used": {"sreality_id": sreality_id, "n_images": n_images},
            "result_count": len(out),
            "queried_at": _now_iso(),
            "data_freshness": None,
            "cost_usd": cost_usd,
        },
    }


def _classify_missing(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    images: list[dict[str, Any]],
    model: str,
) -> tuple[dict[int, dict[str, str]], float]:
    if not image_storage.is_configured():
        raise ClassifyError("R2 is not configured; cannot fetch image bytes for vision")

    r2 = image_storage.R2Client.from_env()
    content: list[dict[str, Any]] = [
        {"type": "text", "text": f"{len(images)} listing images, in order (index 0..N):"}
    ]
    for i, img in enumerate(images):
        content.append({"type": "text", "text": f"Image index {i}:"})
        data = r2.download_bytes(img["storage_path"])
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(data).decode("ascii"),
            },
        })

    system = llm_client.resolve_system_prompt(_PROMPT_KEY)
    response = llm_client.call(
        called_for=_CALLED_FOR,
        messages=[{"role": "user", "content": content}],
        system=system,
        tools=[RECORD_ROOM_TYPES_TOOL],
        model=model,
    )
    rooms = _extract_rooms(response.tool_calls)

    produced: dict[int, dict[str, str]] = {}
    for entry in rooms:
        idx = entry.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(images):
            continue
        rt = entry.get("room_type")
        if rt not in ROOM_TYPES:
            rt = "other"
        conf = entry.get("confidence") if entry.get("confidence") in ("high", "medium", "low") else "low"
        produced[images[idx]["id"]] = {"room_type": rt, "confidence": conf}

    _cache_store(conn, produced, model, response.llm_call_id, response.cost_usd, len(produced))
    return produced, float(response.cost_usd or 0.0)


def _fetch_images(
    conn: "psycopg.Connection", sreality_id: int, n_images: int,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, sequence, storage_path FROM images "
            "WHERE sreality_id = %s AND storage_path IS NOT NULL "
            "ORDER BY sequence ASC NULLS LAST, id ASC LIMIT %s",
            (sreality_id, n_images),
        )
        rows = cur.fetchall()
    return [{"id": r[0], "sequence": r[1], "storage_path": r[2]} for r in rows]


def _cache_lookup(
    conn: "psycopg.Connection", image_ids: list[int], model: str,
) -> dict[int, dict[str, str]]:
    if not image_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT image_id, room_type, confidence FROM image_room_classifications "
            "WHERE model = %s AND image_id = ANY(%s)",
            (model, image_ids),
        )
        rows = cur.fetchall()
    return {r[0]: {"room_type": r[1], "confidence": r[2]} for r in rows}


def _cache_store(
    conn: "psycopg.Connection",
    produced: dict[int, dict[str, str]],
    model: str,
    llm_call_id: int,
    cost_usd: float,
    n: int,
) -> None:
    if not produced:
        return
    per_image_cost = (float(cost_usd) / n) if n else None
    with conn.transaction(), conn.cursor() as cur:
        for image_id, rc in produced.items():
            cur.execute(
                "INSERT INTO image_room_classifications "
                "(image_id, room_type, confidence, model, llm_call_id, cost_usd) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (image_id, model) DO UPDATE SET "
                "  room_type = EXCLUDED.room_type, confidence = EXCLUDED.confidence, "
                "  llm_call_id = EXCLUDED.llm_call_id, cost_usd = EXCLUDED.cost_usd, "
                "  created_at = now()",
                (image_id, rc["room_type"], rc["confidence"], model, llm_call_id, per_image_cost),
            )


def _extract_rooms(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matching = [tc for tc in tool_calls if tc.get("name") == "record_room_types"]
    if not matching:
        raise ClassifyError("LLM did not invoke record_room_types; refusing to guess")
    payload = matching[0].get("input") or {}
    rooms = payload.get("rooms")
    if not isinstance(rooms, list):
        raise ClassifyError("record_room_types missing rooms array")
    return rooms
