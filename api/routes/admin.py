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
from scraper.portal import default_config, load_portal_config
from toolkit import filter_registry

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


class UpdateFilterVisibilityIn(BaseModel):
    enabled: bool


class UpdatePortalLimitsIn(BaseModel):
    """Partial update of one portal's operational_limits. Only fields the client
    sends are applied (merged into the existing value); an explicit null on a cap
    field means "unlimited". Use model_dump(exclude_unset=True) to honor that."""

    index_rate: float | None = None
    detail_workers: int | None = None
    detail_rate: float | None = None
    max_detail_per_run: int | None = None
    max_detail_per_category: int | None = None
    min_completeness: float | None = None
    image_workers: int | None = None
    max_image_downloads: int | None = None
    suspicious_stop_window: int | None = None
    suspicious_stop_threshold: float | None = None


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


# --- filter registry ------------------------------------------------------

@router.get("/filter-schema")
def get_filter_schema(
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """The canonical filter registry, with the operator's visibility
    overrides applied in-line.

    Used by the frontend at runtime (Settings matrix, agent-tool docs)
    and as the seed for the build-time codegen in
    `scripts/generate_filter_registry.py`. The committed
    `frontend/src/lib/filterRegistry.generated.ts` mirrors the same
    shape so the SPA can render `<FilterForm>` and `lib/filters.ts`
    URL serialisation without a network hop.
    """
    visibility = filter_registry.visibility_map(conn)
    return filter_registry.registry_to_json(visibility)


@router.get("/filter-visibility")
def get_filter_visibility(
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Read the agenda × filter visibility matrix.

    Rows missing from the table are implicit `enabled=true` — the
    response includes every (agenda, filter) pair the registry
    declares so the Settings UI can render the full matrix from one
    call.
    """
    visibility = filter_registry.visibility_map(conn)
    matrix: list[dict[str, Any]] = []
    for f in filter_registry.all_filters():
        for agenda in sorted(f.agendas):
            matrix.append({
                "agenda": str(agenda),
                "filter_id": f.id,
                "enabled": visibility.get((str(agenda), f.id), True),
            })
    return {"data": matrix}


@router.put("/filter-visibility/{agenda}/{filter_id}")
def put_filter_visibility(
    agenda: str,
    filter_id: str,
    body: UpdateFilterVisibilityIn,
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Toggle one (agenda, filter) cell.

    Validates that the registry declares the filter for the agenda —
    enabling a filter for an agenda that never references it would
    silently do nothing, which is worse than rejecting the request.
    """
    try:
        f = filter_registry.by_id(filter_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"unknown filter id {filter_id!r}",
        )
    try:
        agenda_enum = filter_registry.Agenda(agenda)
    except ValueError:
        raise HTTPException(
            status_code=404, detail=f"unknown agenda {agenda!r}",
        )
    if agenda_enum not in f.agendas:
        raise HTTPException(
            status_code=400,
            detail=(
                f"filter {filter_id!r} is not declared for agenda "
                f"{agenda!r}; the registry would ignore the override."
            ),
        )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO filter_visibility "
            "  (agenda, filter_id, enabled, updated_at, updated_by) "
            "VALUES (%s, %s, %s, now(), %s) "
            "ON CONFLICT (agenda, filter_id) DO UPDATE "
            "  SET enabled = excluded.enabled, "
            "      updated_at = now(), "
            "      updated_by = excluded.updated_by",
            (agenda, filter_id, body.enabled, "settings_ui"),
        )
    return {
        "agenda": agenda,
        "filter_id": filter_id,
        "enabled": body.enabled,
    }


# --- portals: per-portal operational limits (migration 114) ----------------

@router.get("/portals")
def get_portals(conn: Any = Depends(deps.get_db_conn)) -> dict[str, Any]:
    """Every registry portal with its raw limit overrides + the resolved
    (effective) limits + the baked code default, so the Scrapers dashboard can
    render an editable card per portal and show "(from global)" where a value is
    inherited rather than explicitly overridden."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source, label, kind, stage, sort_order, is_enabled, "
            "supports_complete_walk, operational_limits "
            "FROM portals ORDER BY sort_order, source"
        )
        rows = cur.fetchall()
    data: list[dict[str, Any]] = []
    for r in rows:
        source = r[0]
        try:
            effective = _limits_to_dict(load_portal_config(conn, source).limits)
        except Exception:  # noqa: BLE001 - never let one portal break the list
            effective = None
        try:
            baked = _limits_to_dict(default_config(source).limits)
        except ValueError:
            baked = None  # parser-only portal: no baked scraper default
        data.append({
            "source": source,
            "label": r[1],
            "kind": r[2],
            "stage": r[3],
            "sort_order": r[4],
            "is_enabled": r[5],
            "supports_complete_walk": r[6],
            "overrides": r[7],          # raw per-portal jsonb (or null)
            "effective": effective,     # resolved: baked < global < per-portal
            "baked_default": baked,
        })
    return {"data": data}


@router.put("/portals/{source}/limits")
def put_portal_limits(
    source: str,
    body: UpdatePortalLimitsIn,
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Merge the sent limit fields into the portal's operational_limits. The
    before-update trigger records the prior value in portal_limits_history."""
    import json
    patch = body.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(status_code=400, detail="no limit fields provided")
    try:
        _validate_portal_limits(patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT operational_limits FROM portals WHERE source = %s FOR UPDATE",
            (source,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"portal {source!r} not found")
        merged = {**(row[0] or {}), **patch}
        cur.execute(
            "UPDATE portals SET operational_limits = %s::jsonb, "
            "operational_limits_updated_by = %s WHERE source = %s",
            (json.dumps(merged), "scrapers_ui", source),
        )
    effective = _limits_to_dict(load_portal_config(conn, source).limits)
    return {"source": source, "overrides": merged, "effective": effective}


# --- helpers --------------------------------------------------------------

def _limits_to_dict(limits: Any) -> dict[str, Any]:
    from dataclasses import asdict
    return asdict(limits)


def _validate_portal_limits(patch: dict[str, Any]) -> None:
    """Range/type-check operator-edited limits. These drive live scrapes, so a
    bad shape is rejected (400) rather than silently coerced at scrape time."""
    def _is_num(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def _is_int(v: Any) -> bool:
        return isinstance(v, int) and not isinstance(v, bool)

    positive_floats = ("index_rate", "detail_rate")
    positive_ints = ("detail_workers", "image_workers", "suspicious_stop_window")
    optional_positive_ints = ("max_detail_per_run", "max_detail_per_category")
    fractions = ("min_completeness", "suspicious_stop_threshold")

    for k, v in patch.items():
        if k in positive_floats:
            if not _is_num(v) or v <= 0:
                raise ValueError(f"{k} must be a number > 0")
        elif k in positive_ints:
            if not _is_int(v) or v < 1:
                raise ValueError(f"{k} must be an integer >= 1")
        elif k in optional_positive_ints:
            if v is not None and (not _is_int(v) or v < 1):
                raise ValueError(f"{k} must be an integer >= 1 or null (unlimited)")
        elif k == "max_image_downloads":
            if v is not None and (not _is_int(v) or v < 0):
                raise ValueError(f"{k} must be an integer >= 0 or null (unlimited)")
        elif k in fractions:
            if not _is_num(v) or not (0 < v <= 1):
                raise ValueError(f"{k} must be a number in (0, 1]")


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
