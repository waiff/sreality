"""Feedback capture + refinement-lifecycle persistence.

Slice B (this file): insert / read `estimation_feedback` rows.
Slice C: the refinement loop calls back into `update_feedback_status`
to advance the lifecycle (submitted -> refining -> proposed | failed)
as the refiner runs.

Schema is in migration 047. Status transitions are validated at the
application layer — the DB CHECK constraint enforces the enum but
not the allowed transitions, because operator dismissals from any
non-terminal state are legal.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import psycopg

LOG = logging.getLogger(__name__)


_FEEDBACK_COLUMNS: tuple[str, ...] = (
    "id", "estimation_run_id", "feedback_text",
    "submitted_at", "status", "refinement_id",
)


def insert_feedback(
    conn: "psycopg.Connection",
    *,
    estimation_run_id: int,
    feedback_text: str,
    initial_status: str = "submitted",
) -> dict[str, Any]:
    """Insert one feedback row and return it.

    `initial_status` defaults to 'submitted'; callers that fire the
    refiner synchronously pass 'refining' so the row reflects the
    in-flight state from the start.
    """
    cols = "estimation_run_id, feedback_text, status"
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO estimation_feedback ({cols}) "
            f"VALUES (%s, %s, %s) "
            f"RETURNING {', '.join(_FEEDBACK_COLUMNS)}",
            (estimation_run_id, feedback_text, initial_status),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING produced no row")
    return _row_to_dict(row)


def list_feedback_for_run(
    conn: "psycopg.Connection", run_id: int,
) -> list[dict[str, Any]]:
    """Newest-first list of all feedback rows for one run."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_FEEDBACK_COLUMNS)} "
            f"FROM estimation_feedback "
            f"WHERE estimation_run_id = %s "
            f"ORDER BY submitted_at DESC",
            (run_id,),
        )
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def update_feedback_status(
    conn: "psycopg.Connection",
    feedback_id: int,
    *,
    status: str,
    refinement_id: int | None = None,
) -> None:
    """Advance a feedback row's status, optionally linking to a refinement.

    Idempotent — calling with the same target status is fine.
    """
    if refinement_id is None:
        sql = (
            "UPDATE estimation_feedback SET status = %s WHERE id = %s"
        )
        params: tuple[Any, ...] = (status, feedback_id)
    else:
        sql = (
            "UPDATE estimation_feedback "
            "SET status = %s, refinement_id = %s "
            "WHERE id = %s"
        )
        params = (status, refinement_id, feedback_id)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql, params)


def get_feedback(
    conn: "psycopg.Connection", feedback_id: int,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_FEEDBACK_COLUMNS)} "
            f"FROM estimation_feedback WHERE id = %s",
            (feedback_id,),
        )
        row = cur.fetchone()
    return _row_to_dict(row) if row is not None else None


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    out: dict[str, Any] = dict(zip(_FEEDBACK_COLUMNS, row))
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
    return out
