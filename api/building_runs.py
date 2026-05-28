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
    background_tasks: Any | None = None,
) -> dict[str, Any]:
    """B1: parse the URL, INSERT a 'pending' row, schedule extraction as
    a BackgroundTask, return the row immediately.

    Status transitions:
      INSERT pending  →  extracting  →  awaiting_input  (success)
                                    →  failed           (any error)

    The handler returns in ~1 s. The browser navigates to the building
    detail page, which polls until status reaches awaiting_input / failed.

    Apartment URLs (category_main='byt') are rejected with HTTP 400
    BEFORE the row is INSERTed — those go through /estimations.

    When `background_tasks` is None, extraction runs inline (preserves
    behaviour for tests and any caller that wants the row populated
    before reading it back).
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

    if background_tasks is not None:
        background_tasks.add_task(
            _execute_building_extraction_background,
            building_id=building_id,
            sreality_id=int(result.sreality_id),
            parse_warnings=list(result.warnings),
            subject_summary=subject_summary,
            special_instructions=body.special_instructions,
            contextual_text=body.contextual_text,
        )
        return _fetch_building(conn, building_id) or {}

    _execute_building_extraction(
        conn, llm_client,
        building_id=building_id,
        sreality_id=int(result.sreality_id),
        parse_warnings=list(result.warnings),
        subject_summary=subject_summary,
        special_instructions=body.special_instructions,
        contextual_text=body.contextual_text,
    )
    return _fetch_building(conn, building_id) or {}


def _execute_building_extraction_background(
    *,
    building_id: int,
    sreality_id: int,
    parse_warnings: list[str],
    subject_summary: dict[str, Any] | None,
    special_instructions: str | None,
    contextual_text: str | None,
) -> None:
    """Background-task entry: open a fresh connection + LLM client and
    run the extractor. Any uncaught exception flips the row to 'failed'.
    """
    from api import dependencies as deps

    try:
        with deps.open_background_conn() as conn:
            from api.llm_client import LLMClient
            llm_client = LLMClient(conn, providers=deps.get_providers())
            _execute_building_extraction(
                conn, llm_client,
                building_id=building_id,
                sreality_id=sreality_id,
                parse_warnings=parse_warnings,
                subject_summary=subject_summary,
                special_instructions=special_instructions,
                contextual_text=contextual_text,
            )
    except Exception as exc:
        LOG.exception(
            "background building extraction %s crashed", building_id,
        )
        try:
            with deps.open_background_conn() as conn:
                _update_building_fields(
                    conn, building_id,
                    status="failed",
                    error_message=(
                        f"background crash: {type(exc).__name__}: {exc}"
                    )[:1000],
                )
        except Exception:
            LOG.exception(
                "failed to mark building %s failed after background crash",
                building_id,
            )


def _execute_building_extraction(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    *,
    building_id: int,
    sreality_id: int,
    parse_warnings: list[str],
    subject_summary: dict[str, Any] | None,
    special_instructions: str | None,
    contextual_text: str | None,
) -> None:
    """Run the extractor for an already-INSERTed pending row."""
    _update_building_fields(conn, building_id, status="extracting")
    attachments = _fetch_attachments(conn, building_id)

    try:
        envelope = building_extraction.extract_building_units(
            conn,
            llm_client,
            sreality_id=sreality_id,
            special_instructions=special_instructions,
            contextual_text=contextual_text,
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
        return
    except Exception as exc:  # noqa: BLE001
        LOG.exception(
            "building_runs[%s] unexpected extractor error", building_id,
        )
        _update_building_fields(
            conn, building_id,
            status="failed",
            error_message=f"unexpected extractor error: {exc}",
        )
        return

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
    merged_warnings = list(parse_warnings) + list(payload.get("warnings") or [])

    _update_building_fields(
        conn, building_id,
        status="awaiting_input",
        units_proposal=proposal,
        subject_summary=merged_subject_summary,
        warnings=merged_warnings or None,
    )


# --- B2 orchestrator: per-unit fan-out + rollup ----------------------------
#
# On unit confirmation a building lands in status='estimating'. The
# orchestrator fans out one rent + one sale child estimation_runs row per
# confirmed unit, reusing the standard /estimations plumbing
# (create_estimation_run, agent + deterministic modes alike), then sums the
# per-unit results into building-level totals. It is a fan-out + watcher, not
# a new LLM loop: each child runs synchronously through create_estimation_run,
# so when the loop returns every child has reached a terminal status and the
# rollup is exact. Per CLAUDE.md architectural rule #13 and ROADMAP.md B2.

# Rent children run under the operator's apartment estimator skill so a unit
# inside a building is estimated exactly like a standalone apartment (any
# improvement to that skill rolls into the building flow for free). Sale
# children fall back to deterministic mode until a sale-specific skill exists
# (ROADMAP.md B2 "out of scope"); reading a not-yet-seeded app_settings key
# just returns the fallback, so no migration is needed to wire it later.
_DEFAULT_ESTIMATOR_SKILL = "rental_estimator_v1"


def _orchestrate_building_estimations_background(*, building_id: int) -> None:
    """Background-task entry: open a fresh connection + clients and run the
    per-unit fan-out + rollup. Any uncaught exception flips the row to
    'failed' so it can't get stuck in 'estimating'.
    """
    from api import dependencies as deps

    try:
        with deps.open_background_conn() as conn:
            from api.llm_client import LLMClient
            sreality_client = deps.get_sreality_client()
            llm_client = LLMClient(conn, providers=deps.get_providers())
            _run_building_estimations(
                conn, sreality_client, llm_client, building_id=building_id,
            )
    except Exception as exc:
        LOG.exception(
            "background building orchestration %s crashed", building_id,
        )
        try:
            with deps.open_background_conn() as conn:
                _update_building_fields(
                    conn, building_id,
                    status="failed",
                    error_message=(
                        f"orchestration crash: {type(exc).__name__}: {exc}"
                    )[:1000],
                )
        except Exception:
            LOG.exception(
                "failed to mark building %s failed after orchestration crash",
                building_id,
            )


def _run_building_estimations(
    conn: "psycopg.Connection",
    sreality_client: "SrealityClient | None",
    llm_client: "LLMClient | None",
    *,
    building_id: int,
) -> None:
    """Fan out per-unit child estimations for a confirmed building, then roll
    the totals up. Safe to re-enter: if children already exist it just
    re-runs the finalisation step instead of double-fanning-out.
    """
    from api.estimation_runs import _load_app_setting

    row = _fetch_building(conn, building_id)
    if row is None or row.get("status") != "estimating":
        return

    units = row.get("units") or []
    if not units:
        _update_building_fields(
            conn, building_id,
            status="failed",
            error_message="no confirmed units to estimate",
        )
        return

    if _fetch_children(conn, building_id):
        _finalise_building(conn, building_id)
        return

    lat, lng = _building_latlng(row)
    if lat is None or lng is None:
        _update_building_fields(
            conn, building_id,
            status="failed",
            error_message=(
                "building parse has no lat/lng; cannot fan out per-unit "
                "estimations"
            ),
        )
        return

    source = row.get("source") or "ui"
    rent_skill = str(
        _load_app_setting(
            conn, "building_default_estimator_skill", _DEFAULT_ESTIMATOR_SKILL,
        )
    )
    sale_skill = _load_app_setting(conn, "building_sale_estimator_skill", None)

    for unit in units:
        _create_child_estimation(
            conn, sreality_client, llm_client,
            building_id=building_id, unit=unit, estimate_kind="rent",
            lat=lat, lng=lng, source=source, skill=rent_skill,
        )
        _create_child_estimation(
            conn, sreality_client, llm_client,
            building_id=building_id, unit=unit, estimate_kind="sale",
            lat=lat, lng=lng, source=source,
            skill=str(sale_skill) if sale_skill else None,
        )

    _finalise_building(conn, building_id)


def _building_latlng(
    row: dict[str, Any],
) -> tuple[float | None, float | None]:
    """Pull the subject coordinates from the parse output. Prefer the
    compact subject_summary.fields (what the UI shows); fall back to the
    full input_spec.
    """
    def _coord(d: dict[str, Any] | None, key: str) -> float | None:
        if not isinstance(d, dict):
            return None
        v = d.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    fields = (row.get("subject_summary") or {}).get("fields") \
        if isinstance(row.get("subject_summary"), dict) else None
    spec = row.get("input_spec")
    lat = _coord(fields, "lat")
    lng = _coord(fields, "lng")
    if lat is None:
        lat = _coord(spec, "lat")
    if lng is None:
        lng = _coord(spec, "lng")
    return lat, lng


def _create_child_estimation(
    conn: "psycopg.Connection",
    sreality_client: "SrealityClient | None",
    llm_client: "LLMClient | None",
    *,
    building_id: int,
    unit: dict[str, Any],
    estimate_kind: str,
    lat: float,
    lng: float,
    source: str,
    skill: str | None,
) -> dict[str, Any]:
    """INSERT + run one child estimation for a unit, then link it back to
    the parent building. `skill` None means deterministic mode (today's
    sale path); a skill name means agent mode under that skill.
    """
    from api.estimation_runs import create_estimation_run

    use_agent = bool(skill)
    body = s.CreateEstimationIn(
        source=source if source in ("ui", "api", "clickup") else "ui",
        mode="agent" if use_agent else "deterministic",
        skill=skill or "rental_estimator_full_v1",
        estimate_kind="rent" if estimate_kind == "rent" else "sale",
        spec=s.TargetIn(
            lat=lat,
            lng=lng,
            area_m2=unit.get("area_m2"),
            disposition=unit.get("disposition"),
        ),
        category_main="byt",
        category_type="pronajem" if estimate_kind == "rent" else "prodej",
    )
    child = create_estimation_run(
        conn, sreality_client, llm_client, body, background_tasks=None,
    )
    child_id = child.get("id")
    if child_id is not None:
        _link_child_to_building(
            conn, int(child_id),
            building_id=building_id, unit_id=unit.get("unit_id"),
        )
    return child


def _link_child_to_building(
    conn: "psycopg.Connection",
    child_id: int,
    *,
    building_id: int,
    unit_id: str | None,
) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE estimation_runs "
            "SET building_run_id = %s, building_unit_id = %s WHERE id = %s",
            (building_id, unit_id, child_id),
        )


def _finalise_building(
    conn: "psycopg.Connection", building_id: int,
) -> None:
    """Roll per-unit child estimates up to building-level totals once every
    child has reached a terminal status. No-op while any child is still
    running (the orchestrator runs children synchronously, so this is only
    a guard against re-entry on a partially-done set).
    """
    children = _fetch_children(conn, building_id)
    terminal = {"success", "failed"}
    if not children or any(c.get("status") not in terminal for c in children):
        return

    totals = _rollup_totals(children)
    any_success = any(c.get("status") == "success" for c in children)
    fields: dict[str, Any] = {
        "status": "success" if any_success else "failed",
        **totals,
    }
    if not any_success:
        fields["error_message"] = "all per-unit estimations failed"
    _update_building_fields(conn, building_id, **fields)


def _rollup_totals(
    children: list[dict[str, Any]],
) -> dict[str, int | None]:
    """Sum successful per-unit estimates into building-level totals.

    P50 is the straight sum of per-unit point estimates; P25 / P75 sum the
    per-unit IQR endpoints (matches how the operator reads the spreadsheet).
    Only successful children contribute; a percentile with no contributing
    unit stays NULL rather than reading as a misleading zero.
    """
    def _sum(kind: str, p50_key: str, p25_key: str, p75_key: str) -> tuple[
        int | None, int | None, int | None,
    ]:
        rows = [
            c for c in children
            if c.get("estimate_kind") == kind and c.get("status") == "success"
        ]
        p25 = [c[p25_key] for c in rows if c.get(p25_key) is not None]
        p50 = [c[p50_key] for c in rows if c.get(p50_key) is not None]
        p75 = [c[p75_key] for c in rows if c.get(p75_key) is not None]
        return (
            int(sum(p25)) if p25 else None,
            int(sum(p50)) if p50 else None,
            int(sum(p75)) if p75 else None,
        )

    r25, r50, r75 = _sum(
        "rent", "estimated_monthly_rent_czk", "rent_p25_czk", "rent_p75_czk",
    )
    s25, s50, s75 = _sum(
        "sale", "estimated_sale_price_czk", "sale_p25_czk", "sale_p75_czk",
    )
    return {
        "total_rent_p25_czk": r25,
        "total_rent_p50_czk": r50,
        "total_rent_p75_czk": r75,
        "total_sale_p25_czk": s25,
        "total_sale_p50_czk": s50,
        "total_sale_p75_czk": s75,
    }


def sweep_stuck_buildings(
    conn: "psycopg.Connection",
    *,
    older_than_minutes: int = 10,
) -> int:
    """Mark any building_runs in a non-terminal-non-awaiting status older
    than the cutoff as 'failed'. Returns the number of rows updated.

    Recovers from a server restart mid-background-task. We exclude
    'awaiting_input' (the human-in-the-loop pause is intentional). 'pending'
    and 'extracting' cover an interrupted B1 extraction; 'estimating' covers
    an interrupted B2 fan-out — an orphaned orchestration would otherwise
    sit in 'estimating' forever.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE building_runs "
            "SET status = 'failed', "
            "    error_message = coalesce(error_message, "
            "        'interrupted by server restart') "
            "WHERE status IN ('pending', 'extracting', 'estimating') "
            "  AND created_at < now() - make_interval(mins => %s) "
            "RETURNING id",
            (older_than_minutes,),
        )
        return len(cur.fetchall())


def confirm_units(
    conn: "psycopg.Connection",
    building_id: int,
    body: s.ConfirmBuildingUnitsIn,
    sreality_client: "SrealityClient | None" = None,
    llm_client: "LLMClient | None" = None,
    background_tasks: Any | None = None,
) -> dict[str, Any]:
    """Operator confirmation gate: awaiting_input -> estimating, then fan out.

    Rejects 409 if the row is not in awaiting_input. On success it writes
    the confirmed unit list, flips the row to 'estimating', and hands off
    to the B2 orchestrator which fans out one rent + one sale child
    estimation per unit and rolls the totals back up.

    When `background_tasks` is provided the orchestration runs as a
    BackgroundTask (the handler returns the 'estimating' row immediately;
    the detail page polls until success/failed). When it is None the
    orchestration runs inline — used by tests and any caller that wants
    the rolled-up row before reading it back.
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

    if background_tasks is not None:
        background_tasks.add_task(
            _orchestrate_building_estimations_background,
            building_id=building_id,
        )
        return _fetch_building(conn, building_id) or {}

    _run_building_estimations(
        conn, sreality_client, llm_client, building_id=building_id,
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
