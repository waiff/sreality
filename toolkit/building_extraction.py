"""extract_building_units: structural decomposition of a building listing.

Phase B1 of the building-decomposition track. Reads a single multi-unit
building's latest snapshot (description + structured fields) and up to
`max_images` of its R2-stored photos via Claude vision, and returns
the unit proposal that the operator will confirm before B2's per-unit
estimator fan-out.

Cache lives in `building_unit_extractions`, keyed on
(sreality_id, snapshot_id). A new snapshot (only recorded when content
changed — see CLAUDE.md rule #2) gets a fresh cache entry automatically.

Write-allowed exception per CLAUDE.md toolkit rule #5: same rationale
as `summarize_listing` and `compare_listing_images` — the LLM is the
source of truth, we cache locally so repeats and the inevitable
B1 → B2 round-trip don't re-bill the Anthropic API.

This is the EXTRACTOR. It identifies the units the building contains;
it does NOT estimate rent or sale price. Per-unit estimation in B2
runs under the existing apartment estimator skill (sourced from
`app_settings.building_default_estimator_skill`) so apartment
estimations inside buildings stay consistent with standalone
apartment estimations.
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


_SYSTEM_PROMPT_KEY = "llm_building_extractor_system_prompt"
_MODEL_KEY = "llm_building_extractor_model"
_MAX_IMAGES_KEY = "llm_building_extractor_max_images"
_CALLED_FOR = "extract_building_units"

_DEFAULT_MAX_IMAGES = 8

_CONDITION_VALUES = (
    "novostavba", "po_rekonstrukci", "velmi_dobry",
    "dobry", "pred_rekonstrukci", "k_demolici", "unknown",
)
_CONSTRUCTION_VALUES = (
    "cihla", "panel", "skelet", "drevostavba", "smiseny", "unknown",
)
_SOURCE_VALUES = ("description", "floor_plan", "both", "user_added")
_CONFIDENCE_VALUES = ("high", "medium", "low")


class BuildingExtractionError(RuntimeError):
    """Raised when the unit proposal cannot be produced.

    Covers: no listing row, no snapshot, no images AND no description,
    LLM refusal (no tool call, wrong tool call, missing required field).
    """


def _unit_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "unit_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 50,
                "description": "Stable id 'u1' / 'u2' / ... in display order (ground floor first).",
            },
            "label": {"type": ["string", "null"]},
            "floor": {"type": ["string", "null"]},
            "area_m2": {"type": ["number", "null"], "minimum": 0},
            "disposition": {"type": ["string", "null"]},
            "condition": {
                "type": ["string", "null"],
                "enum": [*_CONDITION_VALUES, None],
            },
            "is_potential": {"type": "boolean"},
            "source": {
                "type": ["string", "null"],
                "enum": [*_SOURCE_VALUES, None],
            },
            "notes": {"type": ["string", "null"], "maxLength": 200},
        },
        "required": [
            "unit_id", "label", "floor", "area_m2", "disposition",
            "condition", "is_potential", "source", "notes",
        ],
    }


def _building_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "floor_count": {"type": ["integer", "null"], "minimum": 0},
            "has_attic": {"type": ["boolean", "null"]},
            "year_built": {"type": ["integer", "null"]},
            "construction_type": {
                "type": ["string", "null"],
                "enum": [*_CONSTRUCTION_VALUES, None],
            },
            "total_area_m2": {"type": ["number", "null"], "minimum": 0},
            "condition": {
                "type": ["string", "null"],
                "enum": [*_CONDITION_VALUES, None],
            },
            "notes": {"type": ["string", "null"], "maxLength": 200},
        },
        "required": [
            "floor_count", "has_attic", "year_built",
            "construction_type", "total_area_m2", "condition", "notes",
        ],
    }


RECORD_BUILDING_UNITS_TOOL: dict[str, Any] = {
    "name": "record_building_units",
    "description": (
        "Record the structural decomposition of a Czech multi-unit "
        "building listing. Call exactly once."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "units": {
                "type": "array",
                "items": _unit_schema(),
                "minItems": 1,
                "maxItems": 30,
            },
            "building": _building_schema(),
            "confidence": {
                "type": "string",
                "enum": list(_CONFIDENCE_VALUES),
            },
            "warnings": {
                "type": "array",
                "items": {"type": "string", "maxLength": 200},
                "minItems": 0,
                "maxItems": 5,
            },
        },
        "required": ["units", "building", "confidence", "warnings"],
    },
}


def extract_building_units(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    sreality_id: int,
    snapshot_id: int | None = None,
    max_images: int | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    from toolkit import _now_iso

    snapshot = _resolve_snapshot(conn, sreality_id, snapshot_id)
    if snapshot is None:
        raise BuildingExtractionError(
            f"no snapshot found for sreality_id={sreality_id}"
            + (f", snapshot_id={snapshot_id}" if snapshot_id is not None else "")
        )

    resolved_snapshot_id = snapshot["id"]
    resolved_max_images = (
        max_images
        if max_images is not None
        else _resolve_max_images(conn)
    )

    cache_hit = False
    if not force_refresh:
        cached = _cache_lookup(conn, sreality_id, resolved_snapshot_id)
        if cached is not None:
            cache_hit = True
            data = cached
        else:
            data = _produce_extraction(
                conn, llm_client, sreality_id, snapshot, resolved_max_images,
            )
    else:
        data = _produce_extraction(
            conn, llm_client, sreality_id, snapshot, resolved_max_images,
        )

    return {
        "data": {
            "sreality_id": sreality_id,
            "snapshot_id": resolved_snapshot_id,
            "units": data["units"],
            "building": data["building"],
            "confidence": data["confidence"],
            "warnings": data["warnings"],
            "n_images": data["n_images"],
            "model": data["model"],
            "cost_usd": data["cost_usd"],
            "cache_hit": cache_hit,
        },
        "metadata": {
            "tool": "extract_building_units",
            "filters_used": {
                "sreality_id": sreality_id,
                "snapshot_id": snapshot_id,
                "max_images": resolved_max_images,
                "force_refresh": force_refresh,
            },
            "result_count": len(data["units"]),
            "queried_at": _now_iso(),
            "data_freshness": snapshot["scraped_at"].isoformat(),
        },
    }


def _produce_extraction(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    sreality_id: int,
    snapshot: dict[str, Any],
    max_images: int,
) -> dict[str, Any]:
    listing = _fetch_listing(conn, sreality_id)
    image_blocks, n_images, fallback_warning = _build_image_blocks(
        conn, sreality_id, max_images,
    )
    text_payload = _build_text_payload(listing, snapshot)

    content: list[dict[str, Any]] = [{"type": "text", "text": text_payload}]
    if image_blocks:
        content.append({
            "type": "text",
            "text": f"Photos and floor plans ({n_images}):",
        })
        content.extend(image_blocks)
    content.append({
        "type": "text",
        "text": (
            "Decompose this building into apartment units per the system "
            "prompt. Call record_building_units exactly once."
        ),
    })

    system = llm_client.resolve_system_prompt(_SYSTEM_PROMPT_KEY)
    model = llm_client.resolve_model(_MODEL_KEY)

    response = llm_client.call(
        called_for=_CALLED_FOR,
        messages=[{"role": "user", "content": content}],
        system=system,
        tools=[RECORD_BUILDING_UNITS_TOOL],
        model=model,
    )
    payload = _extract_tool_call(response.tool_calls)

    units = _normalize_units(payload["units"])
    building = payload["building"]
    confidence = payload["confidence"]
    warnings = list(payload.get("warnings") or [])
    if fallback_warning:
        warnings.append(fallback_warning)
        if confidence == "high":
            confidence = "medium"

    _cache_store(
        conn,
        sreality_id=sreality_id,
        snapshot_id=snapshot["id"],
        units=units,
        building=building,
        confidence=confidence,
        warnings=warnings,
        n_images=n_images,
        model=response.model,
        llm_call_id=response.llm_call_id,
        cost_usd=response.cost_usd,
    )
    return {
        "units": units,
        "building": building,
        "confidence": confidence,
        "warnings": warnings,
        "n_images": n_images,
        "model": response.model,
        "cost_usd": float(response.cost_usd) if response.cost_usd is not None else None,
    }


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
        "area_m2, estate_area, usable_area, locality, district, "
        "building_type, condition, energy_rating, ownership "
        "FROM listings WHERE sreality_id = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (sreality_id,))
        row = cur.fetchone()
    if row is None:
        raise BuildingExtractionError(
            f"listing sreality_id={sreality_id} has snapshot but no listings row"
        )
    keys = (
        "category_main", "category_type", "price_czk", "price_unit",
        "area_m2", "estate_area", "usable_area", "locality", "district",
        "building_type", "condition", "energy_rating", "ownership",
    )
    return dict(zip(keys, row))


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


def _build_image_blocks(
    conn: "psycopg.Connection",
    sreality_id: int,
    max_images: int,
) -> tuple[list[dict[str, Any]], int, str | None]:
    """Returns (blocks, n_images, fallback_warning_or_None).

    If R2 isn't configured or the listing has no R2-stored images,
    returns ([], 0, warning) so the extractor still runs against the
    description alone. Per ROADMAP B1: the function must never crash
    the building flow just because the image-download phase hasn't
    caught up.
    """
    if not image_storage.is_configured():
        return [], 0, (
            "R2 is not configured; building decomposition ran "
            "against the description text only."
        )
    keys = _fetch_image_keys(conn, sreality_id, max_images)
    if not keys:
        return [], 0, (
            "No R2-stored images for this listing yet; building "
            "decomposition ran against the description text only."
        )
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
    return blocks, len(blocks), None


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
        if tc.get("name") == "record_building_units"
    ]
    if not matching:
        raise BuildingExtractionError(
            "LLM did not invoke record_building_units; refusing to guess"
        )
    if len(matching) > 1:
        raise BuildingExtractionError(
            "LLM invoked record_building_units more than once"
        )
    payload = matching[0].get("input") or {}
    if not isinstance(payload, dict):
        raise BuildingExtractionError("record_building_units input was not an object")
    for key in ("units", "building", "confidence", "warnings"):
        if key not in payload:
            raise BuildingExtractionError(
                f"record_building_units missing field: {key}"
            )
    units = payload["units"]
    if not isinstance(units, list) or not units:
        raise BuildingExtractionError(
            "record_building_units returned no units"
        )
    return payload


def _normalize_units(units: list[Any]) -> list[dict[str, Any]]:
    """Coerce each unit to a dict with stable key order + filled defaults."""
    out: list[dict[str, Any]] = []
    for idx, raw in enumerate(units, start=1):
        if not isinstance(raw, dict):
            raise BuildingExtractionError(
                f"unit at index {idx} was not an object"
            )
        unit_id = raw.get("unit_id") or f"u{idx}"
        out.append({
            "unit_id": str(unit_id),
            "label": raw.get("label"),
            "floor": raw.get("floor"),
            "area_m2": raw.get("area_m2"),
            "disposition": raw.get("disposition"),
            "condition": raw.get("condition"),
            "is_potential": bool(raw.get("is_potential", False)),
            "source": raw.get("source"),
            "notes": raw.get("notes"),
        })
    return out


def _resolve_max_images(conn: "psycopg.Connection") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM app_settings WHERE key = %s",
            (_MAX_IMAGES_KEY,),
        )
        row = cur.fetchone()
    if row is None:
        return _DEFAULT_MAX_IMAGES
    value = row[0]
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return _DEFAULT_MAX_IMAGES


def _cache_lookup(
    conn: "psycopg.Connection",
    sreality_id: int,
    snapshot_id: int,
) -> dict[str, Any] | None:
    sql = (
        "SELECT units, building, confidence, warnings, n_images, "
        "model, cost_usd "
        "FROM building_unit_extractions "
        "WHERE sreality_id = %s AND snapshot_id = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (sreality_id, snapshot_id))
        row = cur.fetchone()
    if row is None:
        return None
    units = row[0]
    if not isinstance(units, list) or not units:
        return None
    return {
        "units": units,
        "building": row[1],
        "confidence": row[2],
        "warnings": list(row[3] or []),
        "n_images": int(row[4] or 0),
        "model": row[5],
        "cost_usd": float(row[6]) if row[6] is not None else None,
    }


def _cache_store(
    conn: "psycopg.Connection",
    *,
    sreality_id: int,
    snapshot_id: int,
    units: list[dict[str, Any]],
    building: dict[str, Any],
    confidence: str,
    warnings: list[str],
    n_images: int,
    model: str,
    llm_call_id: int,
    cost_usd: float,
) -> None:
    sql = (
        "INSERT INTO building_unit_extractions "
        "(sreality_id, snapshot_id, units, building, confidence, "
        " warnings, n_images, model, llm_call_id, cost_usd) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (sreality_id, snapshot_id) DO UPDATE SET "
        " units = EXCLUDED.units, "
        " building = EXCLUDED.building, "
        " confidence = EXCLUDED.confidence, "
        " warnings = EXCLUDED.warnings, "
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
                sreality_id, snapshot_id,
                _Jsonb(units), _Jsonb(building),
                confidence, _Jsonb(warnings),
                n_images, model, llm_call_id, cost_usd,
            ),
        )
