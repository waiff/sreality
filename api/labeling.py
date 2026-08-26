"""Border-case flagging — `image_border_cases` (migration 310): even a human
isn't confident about an image's room/plan classification. Independent of
any tag decision — a border case keeps its place in the labeling grid and
its tri-state cells untouched; it just doesn't count toward Gate 1
(toolkit/tag_annotations.tag_overview's gate_count).

Mounted under `/labeling/*`, admin-gated. Shared by `/new-dedup/labeling`
(frontend/src/lib/useBorderCases.ts) — the only remaining consumer since
/clip-audit's retirement (docs/design/tag-annotation-matrix.md, Wave B).
"""

from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import dependencies as deps

router = APIRouter(prefix="/labeling", tags=["labeling"])


class BorderCaseAction(BaseModel):
    image_id: int


def set_border_case(
    conn: psycopg.Connection, *, image_id: int, created_by: str = "operator",
) -> dict[str, Any]:
    """Flag ONE image as a border case. Idempotent — a repeat flag is a
    no-op, not a second row (image_id is unique)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO image_border_cases (image_id, created_by) VALUES (%s,%s) "
            "ON CONFLICT (image_id) DO NOTHING "
            "RETURNING created_at",
            (image_id, created_by),
        )
        r = cur.fetchone()
        if r is None:
            cur.execute(
                "SELECT created_at FROM image_border_cases WHERE image_id = %s", (image_id,),
            )
            r = cur.fetchone()
    return {"data": {"image_id": image_id, "created_at": r[0]}}


def delete_border_case(conn: psycopg.Connection, *, image_id: int) -> dict[str, Any]:
    """Unflag an image as a border case. No-op if it wasn't flagged."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM image_border_cases WHERE image_id = %s", (image_id,),
        )
        deleted = cur.rowcount
    return {"data": {"deleted": bool(deleted)}}


@router.post("/border-case")
def post_border_case(
    body: BorderCaseAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Flag an image as a border case — even a human isn't confident about it."""
    return set_border_case(conn, image_id=body.image_id)


@router.delete("/border-case")
def delete_border_case_route(
    image_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Unflag an image as a border case."""
    return delete_border_case(conn, image_id=image_id)
