"""Admin endpoints: skills + app_settings + agent tool inventory.

Routes under the `/admin/*` prefix. Admin-gated — the router carries
`Depends(require_admin)`, so each call needs either a Supabase JWT whose
claims carry `is_admin` (top-level or `app_metadata`) or, during the
dual-auth window, the legacy static `API_TOKEN` bearer (which maps to
synthetic admin claims). Fails closed (401/503) when neither is
configured. Writes go through a service-role psycopg connection; the
frontend never touches Postgres directly for these tables.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from api import dependencies as deps
from api import rent_map
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

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(deps.require_admin)],
)


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
    image_workers: int | None = None
    max_image_downloads: int | None = None
    suspicious_stop_window: int | None = None
    suspicious_stop_threshold: float | None = None
    shared_rate_limiter: bool | None = None


class UpdateConditionRegionsIn(BaseModel):
    enabled_region_ids: list[int]


class UpdateClipRegionsIn(BaseModel):
    priority_region_ids: list[int]


class UpdateTagPriorityIn(BaseModel):
    # The largest family has 9 tags; cap well above that (defense-in-depth — normalize_priority
    # drops anything unknown anyway, but reject an oversized payload before deserializing it).
    order: list[str] = Field(default_factory=list, max_length=100)


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


# --- dedup settings registry ----------------------------------------------


@router.get("/dedup-settings")
def get_dedup_settings(
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """The dedup-engine knob registry + each knob's current value (its registry
    default when never edited). One typed source of truth for the Settings panel."""
    from toolkit.dedup_settings import REGISTRY

    with conn.cursor() as cur:
        cur.execute(
            "SELECT key, value FROM app_settings WHERE key = ANY(%s)",
            ([s.key for s in REGISTRY],),
        )
        stored = {row[0]: row[1] for row in cur.fetchall()}
    return {
        "data": [
            {
                "key": s.key, "kind": s.kind, "default": s.default,
                "label": s.label, "group": s.group, "help": s.help,
                "min": s.min, "max": s.max,
                "value": stored.get(s.key, s.default),
                "is_default": s.key not in stored,
            }
            for s in REGISTRY
        ]
    }


@router.put("/dedup-settings/{key}")
def put_dedup_setting(
    key: str,
    body: UpdateAppSettingIn,
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Validate against the registry, then UPSERT — so editing a not-yet-stored
    knob just creates its app_settings row. Only registered keys are writable."""
    import json

    from toolkit.dedup_settings import REGISTRY_BY_KEY, coerce

    setting = REGISTRY_BY_KEY.get(key)
    if setting is None:
        raise HTTPException(status_code=404, detail=f"unknown dedup setting {key!r}")
    try:
        value = coerce(setting, body.value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid value: {exc}") from exc
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings "
            "  (key, value, description, updated_at, updated_by) "
            "VALUES (%s, %s::jsonb, %s, now(), %s) "
            "ON CONFLICT (key) DO UPDATE "
            "  SET value = excluded.value, updated_at = now(), "
            "      updated_by = excluded.updated_by",
            (key, json.dumps(value), setting.label, "settings_ui"),
        )
    return {"key": key, "value": value, "is_default": False}


# --- dedup tag-comparison priorities (per family) -------------------------

@router.get("/dedup-tag-priorities")
def get_dedup_tag_priorities(
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Per-family comparison-tag order for the dedup visual layer: the current order, the
    coded default (= the full valid tag set the operator may reorder), and an edited flag."""
    from toolkit.dedup_priorities import priorities_view

    return {"data": priorities_view(conn)}


@router.put("/dedup-tag-priorities/{family}")
def put_dedup_tag_priority(
    family: str,
    body: UpdateTagPriorityIn,
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Persist one family's reordering (validated to its tag set + completed from the default,
    so no room is ever silently dropped). Other families are untouched."""
    from toolkit.dedup_priorities import set_family_priority

    try:
        order = set_family_priority(conn, family, body.order)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    from toolkit.dedup_engine import default_priority_for_family

    default = list(default_priority_for_family(family))
    return {"family": family, "order": order, "default_order": default,
            "is_default": order == default}


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
            "SELECT source, label, kind, sort_order, is_enabled, "
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
            "sort_order": r[3],
            "is_enabled": r[4],
            "supports_complete_walk": r[5],
            "overrides": r[6],          # raw per-portal jsonb (or null)
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


# --- rent map: MF Cenová mapa nájemného (revision history + ingest) --------

@router.get("/rent-map")
def get_rent_map_status(
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    return {"current": rent_map.current_revision(conn)}


@router.get("/rent-map/revisions")
def get_rent_map_revisions(
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    return {"data": rent_map.list_revisions(conn)}


@router.post("/rent-map/revisions")
def post_rent_map_upload(
    file: UploadFile = File(...),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Operator uploads a Cenová mapa XLSX from Settings. Re-uploading an
    unchanged file (same sha256) is a no-op."""
    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    try:
        return rent_map.ingest_bytes(
            conn, data,
            source_filename=file.filename or "upload.xlsx",
            uploaded_by="settings_ui",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/rent-map/fetch")
def post_rent_map_fetch(
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Fetch the current XLSX from the MF page now and ingest it."""
    try:
        data, filename = rent_map.fetch_latest_xlsx()
    except Exception as exc:  # noqa: BLE001 - network/parse, report as 502
        raise HTTPException(
            status_code=502, detail=f"fetch failed: {exc}",
        ) from exc
    try:
        return rent_map.ingest_bytes(
            conn, data, source_filename=filename, uploaded_by="settings_ui_fetch",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- condition scoring: per-kraj enablement ---------------------------------

_CONDITION_REGIONS_SETTING = "condition_scoring_enabled_region_ids"

# Mirrors the toolkit's "functionally active" definition (rule #4) plus the
# not-yet-scored condition, so the counts here equal what the batch submit
# job would pick up for that kraj.
_UNSCORED_ACTIVE_PREDICATE = (
    "is_active AND last_seen_at > now() - interval '30 days' "
    "AND building_condition_level IS NULL "
    "AND apartment_condition_level IS NULL"
)


@router.get("/condition-scoring/regions")
def get_condition_scoring_regions(
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Per-kraj condition-scoring switchboard: every kraj with its enabled
    flag + the count of active listings still awaiting a condition score.
    The batch submit job reads the same app_settings key, so toggling a
    kraj on here is all it takes for the next scheduled run to drain it."""
    return {"data": _condition_regions_payload(conn)}


@router.put("/condition-scoring/regions")
def put_condition_scoring_regions(
    body: UpdateConditionRegionsIn,
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    import json
    known = {kid for kid, _name in _fetch_kraje(conn)}
    unknown = sorted(set(body.enabled_region_ids) - known)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown kraj ids: {unknown}",
        )
    enabled = sorted(set(body.enabled_region_ids))
    # INSERT, not bare UPDATE: the key may not be seeded yet — first toggle
    # creates it, treating the absent key as the empty list it stands for.
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings "
            "  (key, value, description, updated_at, updated_by) "
            "VALUES (%s, %s::jsonb, %s, now(), %s) "
            "ON CONFLICT (key) DO UPDATE "
            "  SET value = excluded.value, "
            "      updated_at = now(), "
            "      updated_by = excluded.updated_by",
            (
                _CONDITION_REGIONS_SETTING,
                json.dumps(enabled),
                "admin_boundaries ids (level=kraj) the scheduled "
                "condition-scoring batch job drains automatically",
                "settings_ui",
            ),
        )
    return {"data": _condition_regions_payload(conn)}


def _fetch_kraje(conn: "psycopg.Connection") -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name FROM admin_boundaries "
            "WHERE level = 'kraj' ORDER BY name"
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def _condition_regions_payload(conn: "psycopg.Connection") -> dict[str, Any]:
    kraje = _fetch_kraje(conn)
    setting = _fetch_app_setting(conn, _CONDITION_REGIONS_SETTING)
    raw = setting["value"] if setting is not None else []
    enabled_ids = (
        sorted({int(v) for v in raw}) if isinstance(raw, list) else []
    )
    with conn.cursor() as cur:
        # One GROUP BY covers both the per-kraj counts and the NULL-region
        # bucket (listings with no usable coordinates, parked for scoring).
        cur.execute(
            "SELECT region_id, count(*) FROM listings "
            f"WHERE {_UNSCORED_ACTIVE_PREDICATE} "
            "GROUP BY region_id"
        )
        counts = {r[0]: r[1] for r in cur.fetchall()}
    enabled = set(enabled_ids)
    return {
        "regions": [
            {
                "id": kid,
                "name": name,
                "enabled": kid in enabled,
                "unscored_active": counts.get(kid, 0),
            }
            for kid, name in kraje
        ],
        "parked_no_geo": counts.get(None, 0),
        "enabled_region_ids": enabled_ids,
    }


# --- CLIP tagging: per-kraj drain priority ----------------------------------

_CLIP_REGIONS_SETTING = "clip_tagging_priority_region_ids"


@router.get("/clip-tagging/regions")
def get_clip_tagging_regions(
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Per-kraj CLIP-tagging switchboard: every kraj with its priority flag + how many
    of its active listings already have a CLIP tag (coverage). Marking a kraj priority
    makes the scheduled clip_tag runs drain it (and its embeddings, so its dedup cosine
    is ready) BEFORE the global sweep — same app_settings key the backfill reads."""
    return {"data": _clip_regions_payload(conn)}


@router.put("/clip-tagging/regions")
def put_clip_tagging_regions(
    body: UpdateClipRegionsIn,
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    import json
    known = {kid for kid, _name in _fetch_kraje(conn)}
    unknown = sorted(set(body.priority_region_ids) - known)
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown kraj ids: {unknown}")
    priority = sorted(set(body.priority_region_ids))
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings "
            "  (key, value, description, updated_at, updated_by) "
            "VALUES (%s, %s::jsonb, %s, now(), %s) "
            "ON CONFLICT (key) DO UPDATE "
            "  SET value = excluded.value, updated_at = now(), "
            "      updated_by = excluded.updated_by",
            (
                _CLIP_REGIONS_SETTING,
                json.dumps(priority),
                "admin_boundaries ids (level=kraj) the scheduled CLIP tagging "
                "drains before the global sweep",
                "settings_ui",
            ),
        )
    return {"data": _clip_regions_payload(conn)}


def _clip_regions_payload(conn: "psycopg.Connection") -> dict[str, Any]:
    kraje = _fetch_kraje(conn)
    setting = _fetch_app_setting(conn, _CLIP_REGIONS_SETTING)
    raw = setting["value"] if setting is not None else []
    priority_ids = sorted({int(v) for v in raw}) if isinstance(raw, list) else []
    with conn.cursor() as cur:
        # The kraj's active-listing volume — the signal for picking what to drain first.
        # (Per-kraj tag COVERAGE would need a 5.2M-image aggregation; overall tagging
        # progress lives in the /dedup pipeline overview instead.)
        cur.execute(
            "SELECT region_id, count(*) FROM listings WHERE is_active GROUP BY region_id"
        )
        counts = {r[0]: int(r[1]) for r in cur.fetchall()}
    priority = set(priority_ids)
    return {
        "regions": [
            {
                "id": kid, "name": name, "priority": kid in priority,
                "active_listings": counts.get(kid, 0),
            }
            for kid, name in kraje
        ],
        "parked_no_geo": counts.get(None, 0),
        "priority_region_ids": priority_ids,
    }


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
    fractions = ("suspicious_stop_threshold",)

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
        elif k == "shared_rate_limiter":
            # The scrape-time coercer (scraper.portal._as_bool) skips non-bools,
            # so reject them here where the operator gets a visible 400.
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be a boolean")


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
