"""AUTODEDUP — what the Progress page reads (docs/design/autodedup/PROGRAM.md §12, W1).

Read-only, admin-gated, and the first UI of the program: one row per ITERATION of any wave,
newest first, plus the header strip's spend-to-date against the $200 program cap (D2). The
numbers are read from `autodedup.iterations` exactly as the lane wrote them — cost comes from
`llm_calls` at lane time (E32) and is never forecast here.

The store lives in migration 528. Until it is applied every route answers 200 with
`store_ready: false` and `data: null`, so an un-migrated database shows "the store is not
created yet" rather than a 500 — the probe-first idiom of api/routes/new_dedup_candidates.py.

Nothing here writes anything: the whole program is shadow mode (D4), and the only writer of
this table is the lane itself.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query

from api import dependencies as deps
from autodedup import progress_sql as psql

try:  # the two SQLSTATEs a missing store raises, if the catalog probe ever misses it
    from psycopg import errors as _pg_errors

    _MISSING_RELATION: tuple[type[BaseException], ...] = (
        _pg_errors.UndefinedTable,
        _pg_errors.InvalidSchemaName,
    )
except Exception:  # noqa: BLE001 — psycopg absent (tests run on fake connections)
    _MISSING_RELATION = ()

router = APIRouter(
    prefix="/autodedup",
    tags=["autodedup"],
    dependencies=[Depends(deps.require_admin)],
)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# D2, the program's spend gate: $25 is the hard per-run cap wired into a lane's `--max-usd`,
# $200 the total for the whole program. They are served next to the spend so the header strip
# reads the cap from the server that also reports the dollars, not from a number retyped in
# the page.
RUN_CAP_USD = 25.0
PROGRAM_CAP_USD = 200.0


def _not_ready() -> dict[str, Any]:
    return {"data": None, "store_ready": False}


def store_ready(conn: Any) -> bool:
    """Does the store of migration 528 exist? `to_regclass` answers NULL for a missing
    relation instead of raising, and is asked BEFORE any other query."""
    with conn.cursor() as cur:
        cur.execute(psql.AUTODEDUP_STORE_READY_SQL)
        row = cur.fetchone()
    return bool(row and row[0])


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _row(columns: tuple[str, ...], row: tuple[Any, ...]) -> dict[str, Any]:
    return {name: _jsonable(value) for name, value in zip(columns, row)}


def _fetch(conn: Any, sql: str, params: dict[str, Any] | None = None) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql, params)
        return list(cur.fetchall())


@router.get("/iterations")
def iterations(
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    after: int | None = Query(None),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """One keyset page of the program ledger, newest first.

    `after` is the previous page's `next_after_id`; absent, the first page. One row over the
    asked-for size is fetched and dropped, so `has_more` is a fact rather than the guess a
    full page would be at the exact end of the table.
    """
    if not store_ready(conn):
        return _not_ready()
    try:
        rows = _fetch(
            conn,
            psql.AUTODEDUP_ITERATIONS_SQL,
            {"after_id": after, "limit": limit + 1},
        )
    except _MISSING_RELATION:
        return _not_ready()
    has_more = len(rows) > limit
    items = [_row(psql.ITERATION_COLUMNS, r) for r in rows[:limit]]
    return {
        "data": {
            "items": items,
            "has_more": has_more,
            "next_after_id": items[-1]["id"] if (items and has_more) else None,
        },
        "store_ready": True,
    }


@router.get("/stats")
def stats(conn: Any = Depends(deps.get_db_conn)) -> dict[str, Any]:
    """The header strip: iterations run, dollars spent to date against the D2 caps, when the
    last pass moved, and the per-wave breakdown. Summed from the one per-wave statement so the
    headline and the table cannot disagree.

    "Waves closed vs open" (§12) is deliberately NOT here: closing a wave is a gate decision
    taken in PROGRAM.md §14, not a state the ledger carries — `status` is per iteration, and a
    wave whose every pass read `done` may still be open. Mode is likewise a constant of the
    program (SHADOW, D4), not a column.
    """
    if not store_ready(conn):
        return _not_ready()
    try:
        rows = _fetch(conn, psql.AUTODEDUP_STATS_SQL)
    except _MISSING_RELATION:
        return _not_ready()
    waves: list[dict[str, Any]] = []
    n_iterations = 0
    total_cost = Decimal("0")
    last_at: Any = None
    for r in rows:
        row = dict(zip(psql.WAVE_COLUMNS, r))
        n = int(row["n"] or 0)
        # Summed as the numeric(10,4) it is stored as, floated once at the end: adding the
        # floats of many waves is not the same number as adding the numerics.
        raw = row["cost_usd"]
        cost_exact = raw if isinstance(raw, Decimal) else Decimal(str(raw or 0))
        cost = float(cost_exact)
        n_iterations += n
        total_cost += cost_exact
        # Compared as timestamps, not as their rendered strings: the isoformat of two
        # offsets orders lexicographically by the wrong key.
        if row["last_at"] is not None and (last_at is None or row["last_at"] > last_at):
            last_at = row["last_at"]
        waves.append(
            {
                "wave": row["wave"],
                "n": n,
                "last_status": row["last_status"],
                "cost_usd": cost,
            }
        )
    return {
        "data": {
            "n_iterations": n_iterations,
            "total_cost_usd": round(float(total_cost), 4),
            "run_cap_usd": RUN_CAP_USD,
            "program_cap_usd": PROGRAM_CAP_USD,
            "last_iteration_at": _jsonable(last_at),
            "waves": waves,
        },
        "store_ready": True,
    }
