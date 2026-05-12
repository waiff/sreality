"""Persistence for /buildings endpoints (Phase B0).

B0 ships schemas + read endpoints + one minimal POST that inserts a
`status='pending'` shell so the read path can be exercised before B1
lands the URL-parse + agent extractor. The full lifecycle
(pending → extracting → awaiting_input → estimating → success/failed)
is encoded in the migration's CHECK constraint; B0 only ever writes
'pending'. See CLAUDE.md architectural rule #13.

Children (per-unit estimation_runs rows) are surfaced on the detail
response via a side-query; the parent never duplicates child fields.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from api import schemas as s

if TYPE_CHECKING:
    import psycopg


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
)

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

    B1 replaces this with `create_building_run_from_url` that parses
    the URL, populates source_* / input_spec / subject_summary, and
    kicks off the extractor synchronously.
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
    )
    return _fetch_building(conn, building_id) or {}


def get_building_run(
    conn: "psycopg.Connection", building_id: int,
) -> dict[str, Any] | None:
    row = _fetch_building(conn, building_id)
    if row is None:
        return None
    row["children"] = _fetch_children(conn, building_id)
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
