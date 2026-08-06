"""NEW DEDUP simulation settings — admin CRUD over the
`toolkit/dedup_sim_settings.py` registry (docs/design/new-dedup/PROGRAM.md, Wave 1).

Mounted under `/new-dedup/*`, admin-gated. The live consumer is the NEW DEDUP
Settings page. Read-heavy surface: GET returns the full registry + effective
values in one call so the page renders without N requests; PUT/DELETE touch
one setting at a time.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import dependencies as deps
from toolkit import dedup_sim_settings as dss

router = APIRouter(
    prefix="/new-dedup", tags=["new-dedup"], dependencies=[Depends(deps.require_admin)]
)


class UpdateSettingIn(BaseModel):
    value: Any


def _single(conn: Any, key: str) -> dict[str, Any]:
    return next(r for r in dss.list_with_metadata(conn) if r["key"] == key)


@router.get("/settings")
def list_settings(conn: Any = Depends(deps.get_db_conn)) -> dict[str, Any]:
    return {"data": dss.list_with_metadata(conn)}


@router.put("/settings/{key}")
def put_setting(
    key: str, body: UpdateSettingIn, conn: Any = Depends(deps.get_db_conn)
) -> dict[str, Any]:
    if key not in dss.REGISTRY:
        raise HTTPException(status_code=404, detail=f"setting {key!r} not found")
    try:
        dss.update_setting(conn, key, body.value, updated_by="settings_ui")
    except dss.SettingValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _single(conn, key)


@router.delete("/settings/{key}")
def delete_setting_override(
    key: str, conn: Any = Depends(deps.get_db_conn)
) -> dict[str, Any]:
    """Revert one setting to its registry default by dropping the override row."""
    if key not in dss.REGISTRY:
        raise HTTPException(status_code=404, detail=f"setting {key!r} not found")
    dss.reset_setting(conn, key)
    return _single(conn, key)
