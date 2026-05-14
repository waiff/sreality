"""Admin endpoints: skills + app_settings + agent tool inventory.

Routes registered under the `/admin/*` prefix. Per slice-1 design
the entire prefix is exempted from the API_TOKEN bearer gate (same
exemption category as /health) — the private Railway URL is the
security perimeter. This is documented in CLAUDE.md alongside the
/health exemption and is intentionally narrow: every other endpoint
on the API still requires the bearer token.

All writes still flow through this service-side Python with a
service-role psycopg connection. The frontend never touches Postgres
directly for these tables.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import dependencies as deps
from api.agent import list_agent_tools
from api.skills import (
    SkillNotFound,
    SkillValidationError,
    list_skills,
    load_skill,
    update_skill,
)

if TYPE_CHECKING:
    import psycopg

router = APIRouter(prefix="/admin", tags=["admin"])


# --- request schemas ------------------------------------------------------

class UpdateSkillIn(BaseModel):
    description: str | None = None
    system_prompt: str | None = None
    allowed_tools: list[str] | None = None
    preferred_model: dict[str, str] | None = None
    limits: dict[str, Any] | None = None


class UpdateAppSettingIn(BaseModel):
    value: Any  # jsonb shape; the caller knows what each key holds


# --- skills ---------------------------------------------------------------

@router.get("/skills")
def get_skills(
    include_archived: bool = False,
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """List skills.

    Archived skills (`archived_at IS NOT NULL`) are hidden by
    default so the Settings page and new-estimation pickers focus
    on the active set. Pass `?include_archived=true` to see the
    full history (referenced by past estimations + the slice C
    refiner's `skill_refinements.skill_name` FK).
    """
    skills = list_skills(conn, include_archived=include_archived)
    return {"data": [_skill_to_dict(s) for s in skills]}


@router.get("/skills/{name}")
def get_skill(
    name: str, conn: Any = Depends(deps.get_db_conn)
) -> dict[str, Any]:
    try:
        skill = load_skill(conn, name)
    except SkillNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _skill_to_dict(skill)


@router.put("/skills/{name}")
def put_skill(
    name: str,
    body: UpdateSkillIn,
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    fields = {
        k: v for k, v in body.model_dump(exclude_none=True).items()
    }
    try:
        skill = update_skill(conn, name, fields, updated_by="settings_ui")
    except SkillValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SkillNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _skill_to_dict(skill)


# --- app_settings ---------------------------------------------------------

@router.get("/app_settings")
def get_app_settings(
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT key, value, description, updated_at "
            "FROM app_settings ORDER BY key"
        )
        rows = cur.fetchall()
    return {
        "data": [
            {
                "key": r[0],
                "value": r[1],
                "description": r[2],
                "updated_at": _iso(r[3]),
            }
            for r in rows
        ]
    }


@router.get("/app_settings/{key}")
def get_app_setting(
    key: str, conn: Any = Depends(deps.get_db_conn)
) -> dict[str, Any]:
    row = _fetch_app_setting(conn, key)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"app_settings key {key!r} not found"
        )
    return row


@router.put("/app_settings/{key}")
def put_app_setting(
    key: str,
    body: UpdateAppSettingIn,
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    import json
    if _fetch_app_setting(conn, key) is None:
        raise HTTPException(
            status_code=404, detail=f"app_settings key {key!r} not found"
        )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE app_settings SET value = %s::jsonb, updated_at = now(), "
            "updated_by = %s WHERE key = %s",
            (json.dumps(body.value), "settings_ui", key),
        )
    row = _fetch_app_setting(conn, key)
    assert row is not None
    return row


# --- agent tool inventory -------------------------------------------------

@router.get("/tools")
def get_agent_tools() -> dict[str, Any]:
    """The agent's registered tool names + descriptions.

    Lets the Settings page render a checkbox list for the skill's
    allowed_tools field — no need to hand-maintain the canonical
    list in the SPA.
    """
    return {"data": list_agent_tools()}


# --- helpers --------------------------------------------------------------

def _skill_to_dict(skill: Any) -> dict[str, Any]:
    return {
        "name": skill.name,
        "description": skill.description,
        "system_prompt": skill.system_prompt,
        "allowed_tools": list(skill.allowed_tools),
        "preferred_model": dict(skill.preferred_model),
        "limits": {
            "max_iterations": skill.limits.max_iterations,
            "max_cost_usd": skill.limits.max_cost_usd,
            "wall_clock_timeout_s": skill.limits.wall_clock_timeout_s,
        },
        "updated_at": skill.updated_at,
        "archived_at": skill.archived_at,
    }


def _fetch_app_setting(
    conn: "psycopg.Connection", key: str,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT key, value, description, updated_at "
            "FROM app_settings WHERE key = %s",
            (key,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "key": row[0],
        "value": row[1],
        "description": row[2],
        "updated_at": _iso(row[3]),
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
