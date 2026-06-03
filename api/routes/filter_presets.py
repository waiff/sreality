"""FastAPI routes for Browse saved filter presets.

Mounted under `/filter-presets/*`. Bearer-gated by the standard
`require_token` dependency. Straight psycopg CRUD — a preset is a named
filter set restored client-side, so there is no matcher, no background
work, and the `filter_spec` is stored/returned as an opaque dict.

Deliberately decoupled from the Watchdog (`/notifications/*`) surface so a
preset can never fire a notification; see `migrations/150_filter_presets.sql`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import dependencies as deps
from api import filter_presets as fp

router = APIRouter(prefix="/filter-presets", tags=["filter-presets"])


class CreatePresetIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    filter_spec: dict[str, Any]


class UpdatePresetIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    filter_spec: dict[str, Any] | None = None


@router.get("")
def get_presets(
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    rows = fp.list_presets(conn)
    return {"data": rows, "total": len(rows)}


@router.post("")
def post_preset(
    body: CreatePresetIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return fp.create_preset(conn, name=body.name, filter_spec=body.filter_spec)


@router.get("/{preset_id}")
def get_one_preset(
    preset_id: str,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    row = fp.get_preset(conn, preset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="preset not found")
    return row


@router.put("/{preset_id}")
def put_preset(
    preset_id: str,
    body: UpdatePresetIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    row = fp.update_preset(
        conn,
        preset_id,
        name=body.name,
        filter_spec=body.filter_spec,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="preset not found")
    return row


@router.delete("/{preset_id}")
def delete_preset(
    preset_id: str,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    if not fp.delete_preset(conn, preset_id):
        raise HTTPException(status_code=404, detail="preset not found")
    return {"deleted": True}
