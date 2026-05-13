"""Persistence + orchestration for /buildings endpoints.

B0 shipped schemas + read endpoints + a minimal POST that inserts a
`status='pending'` shell. B1 adds the operator-facing
`POST /buildings/from_url` (which routes the URL through the
dispatcher, runs the extractor synchronously, lands in
`awaiting_input`), `POST /buildings/{id}/confirm_units` (the
human-in-the-loop gate that promotes the row to `estimating`), and
`POST /buildings/{id}/re_extract` (force-refresh while in
`awaiting_input`).

Per-unit child estimation fan-out + rollup totals + the
`estimating → success/failed` transition all land in B2. See
CLAUDE.md architectural rule #13 and ROADMAP.md "Building
decomposition track".

Children (per-unit estimation_runs rows) are surfaced on the detail
response via a side-query; the parent never duplicates child fields.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from api import schemas as s
from scraper import source_dispatcher
from toolkit import building_extraction
from toolkit.building_extraction import BuildingExtractionError

if TYPE_CHECKING:
    import psycopg

    from api.llm_client import LLMClient
    from scraper.sreality_client import SrealityClient

LOG = logging.getLogger(__name__)

# Building flow accepts these category_main values. Apartments
# ('byt') don't decompose — those go through /estimations instead.
_BUILDING_CATEGORIES = frozenset({"dum", "komercni"})


_BUILDING_COLUMNS: tuple[str, ...] = (
    "id", "created_at",
    "source", "status",
    "input_url", "input_sreality_id", "input_spec",
    "source_kind", "parse_confidence", "parse_confidence_per_field",
    "source_html",
    "subject_summary",
    "units_proposal", "units",
    "total_rent_p25_czk", "total_rent_p50_czk", "total_rent_p75_czk",
    "total_sale_p25_czk", "total_sale_p50_czk", "total_sale_p75_czk",
    "business_case",
    "warnings", "error_message",
    "special_instructions", "contextual_text",
)

# Statuses where the operator may still mutate text inputs + attachments.
# Once estimation starts (status='estimating' / 'success' / 'failed') the
# inputs are frozen for audit; a re-run is the only way to change them.
EDITABLE_INPUTS_STATUSES = frozenset({"pending", "extracting", "awaiting_input"})

_INSERT_COLUMNS: tuple[str, ...] = tuple(
    c for c in _BUILDING_COLUMNS if c not in ("id", "created_at")
)

_JSONB_COLUMNS: tuple[str, ...] = (
    "input_spec", "parse_confidence_per_field",
    "subject_summary",
    "units_proposal", "units",
    "business_case",
    "warnings",
)

_CHILD_COLUMNS: tuple[str, ...] = (
    "id", "created_at", "status", "estimate_kind", "building_unit_id",
    "estimated_monthly_rent_czk", "rent_p25_czk", "rent_p75_czk",
    "estimated_sale_price_czk", "sale_p25_czk", "sale_p75_czk",
    "confidence", "error_message",
)


def create_building_run(
    conn: "psycopg.Connection", body: s.CreateBuildingIn,
) -> dict[str, Any]:
    """B0 minimal: insert a 'pending' shell, return the row.

    Kept for backwards-compat with existing API consumers. B1's
    operator-facing entry is `create_building_run_from_url`.
    """
    building_id = _insert_building(
        conn,
        source=body.source,
        status="pending",
        input_url=body.input_url,
        input_sreality_id=None,
        input_spec=None,
        source_kind=None,
        parse_confidence=None,
        parse_confidence_per_field=None,
        source_html=None,
        subject_summary=None,
        units_proposal=None,
        units=None,
        total_rent_p25_czk=None,
        total_rent_p50_czk=None,
        total_rent_p75_czk=None,
        total_sale_p25_czk=None,
        total_sale_p50_czk=None,
        total_sale_p75_czk=None,
        business_case=None,
        warnings=None,
        error_message=None,
        special_instructions=None,
        contextual_text=None,
    )
    return _fetch_building(conn, building_id) or {}


def create_building_run_from_url(
    conn: "psycopg.Connection",
    sreality_client: "SrealityClient",
    llm_client: "LLMClient",
    body: s.CreateBuildingFromUrlIn,
) -> dict[str, Any]:
    """B1: parse the URL, run the extractor, persist the row.

    Synchronous: blocks until extraction completes (or fails). v1
    accepts the wall-clock cost; Phase 7 slice 2's async lifecycle
    retrofits polling later.

    Status transitions inside this call:
      INSERT pending  →  extracting  →  awaiting_input  (success)
                                    →  failed           (any error)

    Apartment URLs (category_main='byt') are rejected with HTTP 400
    BEFORE the row is INSERTed — those go through /estimations, and
    we don't want stale `byt` rows polluting the building flow.
    """
    try:
        result = source_dispatcher.parse_listing_url(
            body.url,
            sreality_client=sreality_client,
            llm_client=llm_client,
            conn=conn,
            force_refresh=body.force_refresh,
        )
    except source_dispatcher.ParseError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"could not parse {body.url!r}: {exc}",
        )

    spec = dict(result.spec)
    category_main = spec.get("category_main")
    if category_main == "byt":
        raise HTTPException(
            status_code=400,
            detail=(
                "this URL parses as an apartment (category_main='byt'); "
                "use POST /estimations instead — the building flow only "
                "decomposes houses ('dum') and commercial buildings ('komercni')"
            ),
        )
    if category_main not in _BUILDING_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unsupported category_main={category_main!r} for the "
                f"building flow; expected one of {sorted(_BUILDING_CATEGORIES)}"
            ),
        )

    subject_summary = _subject_summary_from_parse(result, spec)

    building_id = _insert_building(
        conn,
        source=body.source,
        status="pending",
        input_url=body.url,
        input_sreality_id=result.sreality_id,
        input_spec=spec,
        source_kind=result.source_kind,
        parse_confidence=result.parse_confidence,
        parse_confidence_per_field=result.parse_confidence_per_field,
        source_html=result.source_html,
        subject_summary=subject_summary,
        units_proposal=None,
        units=None,
        total_rent_p25_czk=None,
        total_rent_p50_czk=None,
        total_rent_p75_czk=None,
        total_sale_p25_czk=None,
        total_sale_p50_czk=None,
        total_sale_p75_czk=None,
        business_case=None,
        warnings=list(result.warnings) or None,
        error_message=None,
        special_instructions=body.special_instructions,
        contextual_text=body.contextual_text,
    )

    if result.sreality_id is None:
        _update_building_fields(
            conn, building_id,
            status="failed",
            error_message=(
                "building-flow URL did not yield a sreality_id; B1 "
                "extractor needs a persisted listing snapshot to read"
            ),
        )
        return _fetch_building(conn, building_id) or {}

    _update_building_fields(conn, building_id, status="extracting")
    attachments = _fetch_attachments(conn, building_id)

    try:
        envelope = building_extraction.extract_building_units(
            conn,
            llm_client,
            sreality_id=result.sreality_id,
            special_instructions=body.special_instructions,
            contextual_text=body.contextual_text,
            attachments=attachments or None,
        )
    except BuildingExtractionError as exc:
        LOG.warning(
            "building_runs[%s] extraction failed: %s", building_id, exc,
        )
        _update_building_fields(
            conn, building_id,
            status="failed",
            error_message=f"extraction failed: {exc}",
        )
        return _fetch_building(conn, building_id) or {}
    except Exception as exc:  # noqa: BLE001
        LOG.exception(
            "building_runs[%s] unexpected extractor error", building_id,
        )
        _update_building_fields(
            conn, building_id,
            status="failed",
            error_message=f"unexpected extractor error: {exc}",
        )
        return _fetch_building(conn, building_id) or {}

    payload = envelope["data"]
    proposal = {
        "units": payload["units"],
        "building": payload["building"],
        "confidence": payload["confidence"],
        "warnings": payload["warnings"],
        "n_images": payload["n_images"],
        "model": payload["model"],
        "cost_usd": payload["cost_usd"],
        "snapshot_id": payload["snapshot_id"],
    }
    merged_subject_summary = {
        **(subject_summary or {}),
        "building": payload["building"],
    }
    merged_warnings = list(result.warnings) + list(payload.get("warnings") or [])

    _update_building_fields(
        conn, building_id,
        status="awaiting_input",
        units_proposal=proposal,
        subject_summary=merged_subject_summary,
        warnings=merged_warnings or None,
    )
    return _fetch_building(conn, building_id) or {}


def confirm_units(
    conn: "psycopg.Connection",
    building_id: int,
    body: s.ConfirmBuildingUnitsIn,
) -> dict[str, Any]:
    """Operator confirmation gate: awaiting_input -> estimating.

    Rejects 409 if the row is not in awaiting_input — B2's
    orchestrator owns the row from there on.
    """
    row = _fetch_building(conn, building_id)
    if row is None:
        raise HTTPException(status_code=404, detail="building run not found")
    if row["status"] != "awaiting_input":
        raise HTTPException(
            status_code=409,
            detail=(
                f"building is in status={row['status']!r}; confirm_units "
                "only valid when status='awaiting_input'"
            ),
        )
    units = [u.model_dump() for u in body.units]
    _validate_unique_unit_ids(units)
    _update_building_fields(
        conn, building_id,
        status="estimating",
        units=units,
    )
    return _fetch_building(conn, building_id) or {}


def re_extract(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    building_id: int,
) -> dict[str, Any]:
    """Force a fresh extractor pass on the current snapshot.

    Only valid while the row is in awaiting_input — once the operator
    confirms units and B2 starts fanning out child estimations, the
    proposal is frozen.
    """
    row = _fetch_building(conn, building_id)
    if row is None:
        raise HTTPException(status_code=404, detail="building run not found")
    if row["status"] != "awaiting_input":
        raise HTTPException(
            status_code=409,
            detail=(
                f"building is in status={row['status']!r}; re_extract "
                "only valid when status='awaiting_input'"
            ),
        )
    sreality_id = row.get("input_sreality_id")
    if sreality_id is None:
        raise HTTPException(
            status_code=400,
            detail="building row has no input_sreality_id; nothing to re-extract",
        )

    _update_building_fields(conn, building_id, status="extracting")
    attachments = _fetch_attachments(conn, building_id)
    try:
        envelope = building_extraction.extract_building_units(
            conn,
            llm_client,
            sreality_id=int(sreality_id),
            force_refresh=True,
            special_instructions=row.get("special_instructions"),
            contextual_text=row.get("contextual_text"),
            attachments=attachments or None,
        )
    except BuildingExtractionError as exc:
        LOG.warning(
            "building_runs[%s] re_extract failed: %s", building_id, exc,
        )
        _update_building_fields(
            conn, building_id,
            status="failed",
            error_message=f"re_extract failed: {exc}",
        )
        return _fetch_building(conn, building_id) or {}

    payload = envelope["data"]
    proposal = {
        "units": payload["units"],
        "building": payload["building"],
        "confidence": payload["confidence"],
        "warnings": payload["warnings"],
        "n_images": payload["n_images"],
        "model": payload["model"],
        "cost_usd": payload["cost_usd"],
        "snapshot_id": payload["snapshot_id"],
    }
    subject_summary = dict(row.get("subject_summary") or {})
    subject_summary["building"] = payload["building"]
    _update_building_fields(
        conn, building_id,
        status="awaiting_input",
        units_proposal=proposal,
        subject_summary=subject_summary,
    )
    return _fetch_building(conn, building_id) or {}


def get_building_run(
    conn: "psycopg.Connection", building_id: int,
) -> dict[str, Any] | None:
    row = _fetch_building(conn, building_id)
    if row is None:
        return None
    row["children"] = _fetch_children(conn, building_id)
    row["attachments"] = _fetch_attachments(conn, building_id)
    return row


def update_building_inputs(
    conn: "psycopg.Connection",
    building_id: int,
    body: s.UpdateBuildingInputsIn,
) -> dict[str, Any]:
    """Patch operator-supplied text inputs on an editable building_run."""
    row = _fetch_building(conn, building_id)
    if row is None:
        raise HTTPException(status_code=404, detail="building run not found")
    if row["status"] not in EDITABLE_INPUTS_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"building is in status={row['status']!r}; operator inputs "
                "can only be edited while status is in "
                f"{sorted(EDITABLE_INPUTS_STATUSES)}"
            ),
        )
    _update_building_fields(
        conn, building_id,
        special_instructions=body.special_instructions,
        contextual_text=body.contextual_text,
    )
    return get_building_run(conn, building_id) or {}


def _fetch_attachments(
    conn: "psycopg.Connection", building_id: int,
) -> list[dict[str, Any]]:
    from api.attachments import list_attachments
    return list_attachments(conn, building_id)


def assert_editable_for_attachments(
    conn: "psycopg.Connection", building_id: int,
) -> dict[str, Any]:
    """Raise 404 / 409 unless the building is in a status that allows
    attachment mutations. Returns the row on success.
    """
    row = _fetch_building(conn, building_id)
    if row is None:
        raise HTTPException(status_code=404, detail="building run not found")
    if row["status"] not in EDITABLE_INPUTS_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"building is in status={row['status']!r}; attachments can "
                "only be added or removed while status is in "
                f"{sorted(EDITABLE_INPUTS_STATUSES)}"
            ),
        )
    return row


def list_building_runs(
    conn: "psycopg.Connection",
    *,
    source: str | None = None,
    status: str | None = None,
    sreality_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    where: list[str] = []
    params: dict[str, Any] = {}
    if source is not None:
        where.append("source = %(source)s")
        params["source"] = source
    if status is not None:
        where.append("status = %(status)s")
        params["status"] = status
    if sreality_id is not None:
        where.append("input_sreality_id = %(sreality_id)s")
        params["sreality_id"] = sreality_id

    where_sql = "WHERE " + " AND ".join(where) if where else ""
    cols_sql = ", ".join(_BUILDING_COLUMNS)
    list_sql = (
        f"SELECT {cols_sql} FROM building_runs {where_sql} "
        f"ORDER BY created_at DESC LIMIT %(limit)s OFFSET %(offset)s"
    )
    count_sql = f"SELECT count(*) FROM building_runs {where_sql}"
    list_params = {**params, "limit": limit, "offset": offset}

    with conn.cursor() as cur:
        cur.execute(list_sql, list_params)
        rows = cur.fetchall()
        cur.execute(count_sql, params)
        total_row = cur.fetchone()
    total = int(total_row[0]) if total_row else 0
    return {
        "data": [_row_to_dict(_BUILDING_COLUMNS, r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _subject_summary_from_parse(
    result: "source_dispatcher.ParseResult",
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Compact display summary stored on building_runs.subject_summary.

    Mirrors estimation_runs' subject_summary shape — the data the UI
    renders at the top of the detail page when the operator first
    sees the building. Keep it lean; full parse data lives in
    `input_spec` and `source_html`.
    """
    fields_of_interest = (
        "category_main", "category_type", "locality", "district",
        "lat", "lng", "area_m2", "estate_area", "usable_area",
        "building_type", "condition", "energy_rating", "ownership",
        "price_czk", "price_unit",
    )
    parsed_fields: dict[str, Any] = {}
    fx = result.full_extraction
    for key in fields_of_interest:
        if key in spec and spec.get(key) is not None:
            parsed_fields[key] = spec[key]
            continue
        if fx is not None and key in fx:
            env = fx[key]
            if isinstance(env, dict) and "value" in env:
                parsed_fields[key] = env["value"]
    return {
        "source_url": result.source_url,
        "source_kind": result.source_kind,
        "sreality_id": result.sreality_id,
        "fields": parsed_fields,
    }


def _validate_unique_unit_ids(units: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for unit in units:
        uid = unit.get("unit_id")
        if not uid:
            raise HTTPException(
                status_code=400,
                detail="every unit must have a non-empty unit_id",
            )
        if uid in seen:
            raise HTTPException(
                status_code=400,
                detail=f"duplicate unit_id={uid!r} in confirmed unit list",
            )
        seen.add(uid)


def _update_building_fields(
    conn: "psycopg.Connection",
    building_id: int,
    **fields: Any,
) -> None:
    if not fields:
        return
    for k in _JSONB_COLUMNS:
        if k in fields and fields[k] is not None:
            fields[k] = Jsonb(fields[k])
    sets = ", ".join(f"{c} = %({c})s" for c in fields)
    sql = f"UPDATE building_runs SET {sets} WHERE id = %(__id)s"
    params = {**fields, "__id": building_id}
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql, params)


def _insert_building(conn: "psycopg.Connection", **fields: Any) -> int:
    for k in _JSONB_COLUMNS:
        if fields.get(k) is not None:
            fields[k] = Jsonb(fields[k])
    cols_sql = ", ".join(_INSERT_COLUMNS)
    placeholders = ", ".join(f"%({c})s" for c in _INSERT_COLUMNS)
    sql = (
        f"INSERT INTO building_runs ({cols_sql}) "
        f"VALUES ({placeholders}) RETURNING id"
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql, fields)
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT did not return an id")
        return int(row[0])


def _fetch_building(
    conn: "psycopg.Connection", building_id: int,
) -> dict[str, Any] | None:
    cols_sql = ", ".join(_BUILDING_COLUMNS)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {cols_sql} FROM building_runs WHERE id = %s",
            (building_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _row_to_dict(_BUILDING_COLUMNS, row)


def _fetch_children(
    conn: "psycopg.Connection", building_id: int,
) -> list[dict[str, Any]]:
    cols_sql = ", ".join(_CHILD_COLUMNS)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {cols_sql} FROM estimation_runs "
            f"WHERE building_run_id = %s ORDER BY id ASC",
            (building_id,),
        )
        rows = cur.fetchall()
    return [_row_to_dict(_CHILD_COLUMNS, r) for r in rows]


def _row_to_dict(
    cols: tuple[str, ...] | list[str], row: tuple[Any, ...],
) -> dict[str, Any]:
    out: dict[str, Any] = dict(zip(cols, row))
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
    return out
