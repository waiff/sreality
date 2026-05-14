"""Phase B2: per-unit child estimation fan-out + rollup.

Called synchronously from `POST /buildings/{id}/confirm_units` once
the operator has approved the unit list. For each unit:

  1. Build a `TargetSpec` from the parent building's lat/lng + the
     unit's area_m2 / disposition / floor.
  2. INSERT one rent + one sale `estimation_runs` row, both linked
     back via `building_run_id` + `building_unit_id` (mode='agent',
     status='running').
  3. Drive the apartment estimator skill (sourced from
     `app_settings.building_default_estimator_skill` for rent, and
     `app_settings.building_default_sale_estimator_skill` for sale)
     with the parent's operator-supplied `special_instructions`,
     `contextual_text`, and uploaded attachments (so the child agent
     can call `read_floor_plan`).
  4. UPDATE each child to its terminal status.

After every child has settled, sum the successful children's
`rent_p25 / median / p75_czk` into `total_rent_*_czk` and the
successful sale children's `sale_p25 / estimated_sale_price / p75_czk`
into `total_sale_*_czk`, then transition the parent to
`success` / `failed`.

Synchronous by design: matches the existing extraction call's
wall-clock style and avoids adding async infrastructure. With the
agent's per-skill `max_cost_usd` and `wall_clock_timeout_s` caps a
5-unit building takes 4-8 minutes (2 children per unit, 30-60s each).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from api import estimation_runs as er
from toolkit.comparables import ComparableFilters, TargetSpec

if TYPE_CHECKING:
    import psycopg

    from api.llm_client import LLMClient
    from scraper.sreality_client import SrealityClient

LOG = logging.getLogger(__name__)

EstimateKind = Literal["rent", "sale"]

_DEFAULT_RENT_SKILL_KEY = "building_default_estimator_skill"
_DEFAULT_SALE_SKILL_KEY = "building_default_sale_estimator_skill"
_DEFAULT_RENT_SKILL_FALLBACK = "rental_estimator_v1"
_DEFAULT_SALE_SKILL_FALLBACK = "sale_estimator_v1"

_KIND_TO_CATEGORY_TYPE: dict[str, str] = {
    "rent": "pronajem",
    "sale": "prodej",
}


def fan_out_unit_estimations(
    conn: "psycopg.Connection",
    sreality_client: "SrealityClient",
    llm_client: "LLMClient",
    building_run_id: int,
) -> dict[str, Any]:
    """Drive per-unit rent + sale estimations + rollup. Returns the parent row.

    Per-child failures are tolerated — a child agent that raises gets
    its row marked `failed` with the exception message, and the rest
    of the fan-out continues. Rollup uses only successful children.
    If every child fails, the parent transitions to `failed`.

    If a sale skill is missing or misconfigured the sale fan-out is
    skipped (the parent still succeeds on rent alone); rent skill
    misconfiguration is fatal.
    """
    from api.attachments import list_attachments
    from api.building_runs import _fetch_building
    from api.skills import SkillNotFound, load_skill

    parent = _fetch_building(conn, building_run_id)
    if parent is None:
        raise ValueError(f"building_run_id={building_run_id} not found")
    if parent["status"] != "estimating":
        raise ValueError(
            f"fan_out_unit_estimations: parent in status={parent['status']!r}; "
            "expected 'estimating'"
        )

    spec = parent.get("input_spec") or {}
    lat = spec.get("lat")
    lng = spec.get("lng")
    if lat is None or lng is None:
        _mark_parent_failed(
            conn, building_run_id,
            error="parent building has no lat/lng; cannot fan out per-unit estimations",
        )
        return _fetch_building(conn, building_run_id) or {}

    units = list(parent.get("units") or [])
    if not units:
        _mark_parent_failed(
            conn, building_run_id,
            error="parent building has no confirmed units; nothing to estimate",
        )
        return _fetch_building(conn, building_run_id) or {}

    rent_skill_name = _resolve_default_skill(conn, _DEFAULT_RENT_SKILL_KEY, _DEFAULT_RENT_SKILL_FALLBACK)
    try:
        rent_skill = load_skill(conn, rent_skill_name)
    except SkillNotFound:
        _mark_parent_failed(
            conn, building_run_id,
            error=f"building_default_estimator_skill={rent_skill_name!r} is unknown",
        )
        return _fetch_building(conn, building_run_id) or {}

    sale_skill_name = _resolve_default_skill(conn, _DEFAULT_SALE_SKILL_KEY, _DEFAULT_SALE_SKILL_FALLBACK)
    sale_skill: Any = None
    try:
        sale_skill = load_skill(conn, sale_skill_name)
    except SkillNotFound:
        LOG.warning(
            "B2 building_runs[%s] sale skill %r not found; skipping sale fan-out",
            building_run_id, sale_skill_name,
        )

    attachments = list_attachments(conn, building_run_id)
    special_instructions = parent.get("special_instructions")
    contextual_text = parent.get("contextual_text")
    source = parent.get("source") or "ui"
    input_sreality_id = parent.get("input_sreality_id")

    LOG.info(
        "B2 building_runs[%s] fanning out %d units (rent=%r, sale=%r) "
        "with %d attachment(s)",
        building_run_id, len(units),
        rent_skill_name,
        sale_skill_name if sale_skill is not None else None,
        len(attachments),
    )

    tally = {"success": 0, "failed": 0}
    for unit in units:
        for kind, skill in (("rent", rent_skill), ("sale", sale_skill)):
            if skill is None:
                continue
            outcome = _run_one_unit(
                conn, sreality_client, llm_client,
                parent=parent, unit=unit,
                lat=float(lat), lng=float(lng),
                skill=skill,
                estimate_kind=kind,  # type: ignore[arg-type]
                source=source,
                input_sreality_id=input_sreality_id,
                special_instructions=special_instructions,
                contextual_text=contextual_text,
                attachments=attachments,
            )
            tally[outcome] += 1

    rollup_building_estimates(conn, building_run_id)

    LOG.info(
        "B2 building_runs[%s] fan-out complete: %d success / %d failed",
        building_run_id, tally["success"], tally["failed"],
    )
    return _fetch_building(conn, building_run_id) or {}


def rollup_building_estimates(
    conn: "psycopg.Connection",
    building_run_id: int,
) -> None:
    """Sum successful child rent + sale percentiles into the parent row
    and transition the parent to its terminal status.

    Rent and sale are summed independently. If neither family has any
    successful children with a numeric range, the parent fails; if
    either family has at least one good child, the parent succeeds
    and the other family's totals stay null.
    """
    from api.building_runs import _update_building_fields

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT estimate_kind, status,
                   rent_p25_czk, estimated_monthly_rent_czk, rent_p75_czk,
                   sale_p25_czk, estimated_sale_price_czk, sale_p75_czk
            FROM estimation_runs
            WHERE building_run_id = %s
            """,
            (building_run_id,),
        )
        rows = cur.fetchall()

    if not rows:
        _update_building_fields(
            conn, building_run_id,
            status="failed",
            error_message="rollup: no child estimations found",
        )
        return

    rent_totals = _sum_family(rows, kind="rent")
    sale_totals = _sum_family(rows, kind="sale")

    fields: dict[str, Any] = {}
    if rent_totals is not None:
        fields["total_rent_p25_czk"] = rent_totals[0]
        fields["total_rent_p50_czk"] = rent_totals[1]
        fields["total_rent_p75_czk"] = rent_totals[2]
    if sale_totals is not None:
        fields["total_sale_p25_czk"] = sale_totals[0]
        fields["total_sale_p50_czk"] = sale_totals[1]
        fields["total_sale_p75_czk"] = sale_totals[2]

    if rent_totals is None and sale_totals is None:
        n_success = sum(1 for r in rows if r[1] == "success")
        n_failed = sum(1 for r in rows if r[1] == "failed")
        _update_building_fields(
            conn, building_run_id,
            status="failed",
            error_message=(
                f"rollup: no successful child estimations with a numeric range "
                f"(success={n_success}, failed={n_failed})"
            ),
        )
        return

    fields["status"] = "success"
    _update_building_fields(conn, building_run_id, **fields)


def _sum_family(
    rows: list[tuple[Any, ...]],
    *,
    kind: EstimateKind,
) -> tuple[int, int, int] | None:
    """Sum p25 / median / p75 over rows of `kind` that are `success`
    AND have a complete numeric triple. Returns None if no row qualifies.
    """
    if kind == "rent":
        idx = (2, 3, 4)
    else:
        idx = (5, 6, 7)
    p25 = 0
    median = 0
    p75 = 0
    n = 0
    for row in rows:
        if row[0] != kind or row[1] != "success":
            continue
        a, b, c = row[idx[0]], row[idx[1]], row[idx[2]]
        if a is None or b is None or c is None:
            continue
        p25 += int(a)
        median += int(b)
        p75 += int(c)
        n += 1
    if n == 0:
        return None
    return p25, median, p75


def _run_one_unit(
    conn: "psycopg.Connection",
    sreality_client: "SrealityClient",
    llm_client: "LLMClient",
    *,
    parent: dict[str, Any],
    unit: dict[str, Any],
    lat: float,
    lng: float,
    skill: Any,
    estimate_kind: EstimateKind,
    source: str,
    input_sreality_id: int | None,
    special_instructions: str | None,
    contextual_text: str | None,
    attachments: list[dict[str, Any]],
) -> str:
    """Estimate one (unit, estimate_kind) pair. Returns 'success' or 'failed'.

    Inserts the child row (status='running') BEFORE invoking the agent
    so `llm_calls.estimation_run_id` attribution lights up while the
    loop is in flight. Always updates to a terminal status; never
    raises out of this function.
    """
    from api.agent import run_agent_estimation

    unit_id = str(unit.get("unit_id") or "")
    area_m2 = unit.get("area_m2")
    disposition = unit.get("disposition")
    floor = _normalize_floor(unit.get("floor"))
    category_type = _KIND_TO_CATEGORY_TYPE[estimate_kind]

    target = TargetSpec(
        lat=lat,
        lng=lng,
        area_m2=float(area_m2) if area_m2 is not None else None,
        disposition=disposition,
        floor=floor,
        exclude_ids=[input_sreality_id] if input_sreality_id is not None else [],
    )
    filters = ComparableFilters(
        radius_m=er._DEFAULT_RADIUS_M,
        area_band_pct=er._DEFAULT_AREA_BAND_PCT,
        disposition_match=er._DEFAULT_DISPOSITION_MATCH,
        max_age_days=er._default_max_age_days(estimate_kind),
        active_only=er._DEFAULT_ACTIVE_ONLY,
        category_main="byt",
        category_type=category_type,
    )

    input_spec = {
        "lat": lat,
        "lng": lng,
        "area_m2": target.area_m2,
        "disposition": disposition,
        "floor": floor,
        "unit_id": unit_id,
        "from_building_run_id": parent["id"],
    }

    recorder = er.TraceRecorder()
    child_id = er._insert_run(
        conn,
        source=source,
        mode="agent",
        status="running",
        estimate_kind=estimate_kind,
        input_url=None,
        input_sreality_id=None,
        input_spec=input_spec,
        input_purchase_price_czk=None,
        trace=recorder.to_dict("agent running"),
        special_instructions=special_instructions,
        contextual_text=contextual_text,
        building_run_id=parent["id"],
        building_unit_id=unit_id,
    )

    if target.area_m2 is None:
        er._update_run_terminal(
            conn, child_id,
            status="failed",
            trace=recorder.to_dict("skipped: unit has no area_m2"),
            error_message=(
                f"unit {unit_id!r} has no area_m2; per-unit estimation needs "
                "an area to scale price-per-m² comparables"
            ),
        )
        return "failed"

    try:
        agent_result = run_agent_estimation(
            conn, sreality_client, llm_client,
            target, filters, None,
            skill=skill, provider="anthropic",
            recorder=recorder, estimation_run_id=child_id,
            estimate_kind=estimate_kind,
            special_instructions=special_instructions,
            contextual_text=contextual_text,
            building_run_id=parent["id"],
            attachments=attachments or None,
        )
    except Exception as exc:  # noqa: BLE001 — tolerated per docstring
        LOG.warning(
            "B2 building_runs[%s] unit %s (%s) agent raised: %s",
            parent["id"], unit_id, estimate_kind, exc,
        )
        trace = recorder.to_dict(f"agent failed: {type(exc).__name__}")
        er._update_run_terminal(
            conn, child_id,
            status="failed",
            trace=trace,
            error_message=f"{type(exc).__name__}: {exc}"[:1000],
        )
        return "failed"

    d = agent_result.data
    md = agent_result.metadata
    status = "success" if md.get("stop_reason") == "record_estimate" else "failed"

    if status == "success":
        if estimate_kind == "rent" and d.get("estimated_monthly_rent_czk") is None:
            status = "failed"
        elif estimate_kind == "sale" and d.get("estimated_sale_price_czk") is None:
            status = "failed"

    trace = recorder.to_dict(er._agent_summary_line(d, md))
    warnings = list(d.get("warnings") or []) or None
    err: str | None
    if status == "failed":
        err = (
            f"agent halted: {md.get('stop_reason')}"
            if md.get("stop_reason") != "record_estimate"
            else f"agent recorded {estimate_kind} estimate without the required {estimate_kind}_* fields"
        )
    else:
        err = None

    er._update_run_terminal(
        conn, child_id,
        status=status,
        estimated_monthly_rent_czk=d.get("estimated_monthly_rent_czk"),
        rent_p25_czk=d.get("rent_p25_czk"),
        rent_p75_czk=d.get("rent_p75_czk"),
        estimated_sale_price_czk=d.get("estimated_sale_price_czk"),
        sale_p25_czk=d.get("sale_p25_czk"),
        sale_p75_czk=d.get("sale_p75_czk"),
        confidence=d.get("confidence"),
        comparables_used=d.get("comparables_used"),
        trace=trace,
        warnings=warnings,
        error_message=err,
    )
    return status


def _mark_parent_failed(
    conn: "psycopg.Connection",
    building_run_id: int,
    *,
    error: str,
) -> None:
    from api.building_runs import _update_building_fields
    LOG.warning("B2 building_runs[%s] failed: %s", building_run_id, error)
    _update_building_fields(
        conn, building_run_id,
        status="failed",
        error_message=error,
    )


def _resolve_default_skill(
    conn: "psycopg.Connection",
    key: str,
    fallback: str,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM app_settings WHERE key = %s",
            (key,),
        )
        row = cur.fetchone()
    if row is None:
        return fallback
    value = row[0]
    if isinstance(value, str) and value:
        return value
    return fallback


def _normalize_floor(value: Any) -> int | None:
    """Coerce the unit's floor string ('ground' / '1' / '2' / 'attic') to int.

    TargetSpec.floor is typed `int | None`; the agent uses it only as
    an optional filter input, so a soft-coerce is fine — anything we
    can't map cleanly becomes None, the agent runs without the filter.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip().lower()
    if s in ("ground", "prizemi", "přízemí", "0", "1. np"):
        return 0
    if s in ("attic", "podkrovi", "podkroví"):
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None
