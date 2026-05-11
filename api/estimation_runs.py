"""Persistence and trace machinery for /estimations endpoints.

Trace contract (locked at TRACE_SCHEMA_VERSION = 1):

    {
      "version": 1,
      "summary": "<one-line human-readable summary>",
      "steps": [
        {
          "n": 1,                                  # 1-indexed monotonic
          "kind": "tool_call" | "computation" | "reasoning",
          "started_at": "ISO-8601 UTC ms",
          "duration_ms": int,
          "output_summary": {...},                 # NEVER full output
          # tool_call adds:    "tool", "input"
          # computation adds:  "label"
          # reasoning reserved for Phase U4 agent.
        },
        ...
      ]
    }

Full tool outputs (lists of comparables, full distribution stats) live
in dedicated columns on estimation_runs (comparables_used etc.), not
in the trace. The trace stays bounded regardless of cohort size.

Bumping TRACE_SCHEMA_VERSION is a deliberate change — readers must
handle older versions.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from api import schemas as s
from scraper import source_dispatcher
from toolkit import ComparableFilters, TargetSpec

if TYPE_CHECKING:
    import psycopg

    from api.llm_client import LLMClient
    from scraper.sreality_client import SrealityClient

LOG = logging.getLogger(__name__)

TRACE_SCHEMA_VERSION = 1


class StepHandle:
    """Returned by recorder context managers; caller sets the summary."""

    def __init__(self) -> None:
        self.summary: dict[str, Any] = {}

    def set_summary(self, summary: dict[str, Any]) -> None:
        self.summary = summary


class TraceRecorder:
    """Captures tool calls and computations into the trace format."""

    def __init__(self) -> None:
        self._steps: list[dict[str, Any]] = []
        self._n = 0

    @contextlib.contextmanager
    def tool_call(
        self, tool: str, input: dict[str, Any]
    ) -> Iterator[StepHandle]:
        started_at = datetime.now(timezone.utc)
        mono_start = time.monotonic()
        handle = StepHandle()
        try:
            yield handle
        finally:
            self._append(
                kind="tool_call",
                started_at=started_at,
                duration_ms=_ms_since(mono_start),
                fields={"tool": tool, "input": input},
                handle=handle,
            )

    @contextlib.contextmanager
    def computation(self, label: str) -> Iterator[StepHandle]:
        started_at = datetime.now(timezone.utc)
        mono_start = time.monotonic()
        handle = StepHandle()
        try:
            yield handle
        finally:
            self._append(
                kind="computation",
                started_at=started_at,
                duration_ms=_ms_since(mono_start),
                fields={"label": label},
                handle=handle,
            )

    @contextlib.contextmanager
    def reasoning(self) -> Iterator[StepHandle]:
        """Capture one turn of plain-text reasoning emitted by the agent.

        The handle's summary should be set to
            {"text": "<truncated 800 chars>",
             "tool_calls_queued": [<tool names>],
             "provider": "<provider name>"}
        The agent loop owns those fields; the recorder only enforces
        the kind / monotonic step number / timing.
        """
        started_at = datetime.now(timezone.utc)
        mono_start = time.monotonic()
        handle = StepHandle()
        try:
            yield handle
        finally:
            self._append(
                kind="reasoning",
                started_at=started_at,
                duration_ms=_ms_since(mono_start),
                fields={},
                handle=handle,
            )

    def to_dict(self, summary: str) -> dict[str, Any]:
        return {
            "version": TRACE_SCHEMA_VERSION,
            "summary": summary,
            "steps": list(self._steps),
        }

    def _append(
        self,
        *,
        kind: str,
        started_at: datetime,
        duration_ms: int,
        fields: dict[str, Any],
        handle: StepHandle,
    ) -> None:
        self._n += 1
        step: dict[str, Any] = {
            "n": self._n,
            "kind": kind,
            "started_at": started_at.isoformat(timespec="milliseconds"),
            "duration_ms": duration_ms,
            **fields,
            "output_summary": handle.summary,
        }
        self._steps.append(step)


class _NullStepHandle:
    def set_summary(self, summary: dict[str, Any]) -> None:
        return None


class _NullTraceRecorder:
    """No-op recorder used when estimate_yield is called without one.

    Lets estimate_yield use the same `with rec.tool_call(...)` form
    regardless of whether a real recorder was passed, with effectively
    zero overhead and no behavioural change for existing callers.
    """

    @contextlib.contextmanager
    def tool_call(
        self, tool: str, input: dict[str, Any]
    ) -> Iterator[_NullStepHandle]:
        yield _NULL_HANDLE

    @contextlib.contextmanager
    def computation(self, label: str) -> Iterator[_NullStepHandle]:
        yield _NULL_HANDLE

    @contextlib.contextmanager
    def reasoning(self) -> Iterator[_NullStepHandle]:
        yield _NULL_HANDLE

    def to_dict(self, summary: str) -> dict[str, Any]:
        return {
            "version": TRACE_SCHEMA_VERSION,
            "summary": summary,
            "steps": [],
        }


_NULL_HANDLE = _NullStepHandle()
NULL_RECORDER: Any = _NullTraceRecorder()


def _ms_since(mono_start: float) -> int:
    return int((time.monotonic() - mono_start) * 1000)


# --- create / get / list endpoints -----------------------------------------

_RUN_COLUMNS: tuple[str, ...] = (
    "id", "created_at", "source", "mode", "status",
    "input_url", "input_sreality_id", "input_spec",
    "input_purchase_price_czk",
    "estimated_monthly_rent_czk", "rent_p25_czk", "rent_p75_czk",
    "gross_yield_pct", "confidence",
    "comparables_used", "trace", "warnings", "error_message",
    "parent_run_id", "rerun_reason",
    "source_kind", "parse_confidence", "parse_confidence_per_field",
    "source_html",
)

_INSERT_COLUMNS: tuple[str, ...] = tuple(
    c for c in _RUN_COLUMNS if c not in ("id", "created_at")
)

_COST_TOTAL_SUBSELECT = (
    "coalesce("
    "(SELECT sum(cost_usd) FROM llm_calls WHERE estimation_run_id = er.id), "
    "0)::float AS cost_usd_total"
)
_RUN_PROJECTION = (
    ", ".join(f"er.{c}" for c in _RUN_COLUMNS) + ", " + _COST_TOTAL_SUBSELECT
)
_RUN_COLUMNS_OUT: tuple[str, ...] = _RUN_COLUMNS + ("cost_usd_total",)


@dataclass
class _Resolution:
    """The result of turning a CreateEstimationIn body into a target spec.

    Carries the dispatcher's audit fields (source_kind, parse_confidence,
    parse_confidence_per_field, source_html) plus the input-side bookkeeping
    (input_url, input_sreality_id, target_spec). Built once at the top of
    create_estimation_run and reused by both the success and failed-row
    persistence paths.
    """
    input_url: str | None
    input_sreality_id: int | None
    target_spec: dict[str, Any] | None
    source_kind: str | None
    parse_confidence: str | None
    parse_confidence_per_field: dict[str, str] | None
    source_html: str | None
    parse_warnings: list[str] = field(default_factory=list)


_EMPTY_RESOLUTION = _Resolution(
    input_url=None, input_sreality_id=None, target_spec=None,
    source_kind=None, parse_confidence=None,
    parse_confidence_per_field=None, source_html=None,
    parse_warnings=[],
)


def create_estimation_run(
    conn: "psycopg.Connection",
    sreality_client: "SrealityClient",
    llm_client: "LLMClient",
    body: s.CreateEstimationIn,
) -> dict[str, Any]:
    """POST /estimations: resolve input, run the estimate, persist, return row.

    Synchronous deterministic mode goes straight to a terminal status —
    'success' or 'failed' — in a single INSERT. Agent mode uses an
    early-INSERT (status='running') so LLM costs can attribute via
    `llm_calls.estimation_run_id` while the loop is in flight, then
    UPDATEs to the terminal status.
    """
    from api.estimate_yield import estimate_yield

    try:
        resolution = _resolve_input(conn, sreality_client, llm_client, body)
    except Exception as exc:
        LOG.warning("URL parse failed: %s", exc)
        return _persist_failed_run(
            conn, body=body, resolution=_resolution_for_parse_failure(body),
            recorder=TraceRecorder(),
            error_msg=f"parse failed: {type(exc).__name__}: {exc}"[:1000],
            extra_warnings=[],
        )

    target = _build_target(resolution.target_spec)
    filters = _build_filters(body)

    if body.mode == "agent":
        return _run_agent_path(
            conn, sreality_client, llm_client, body,
            resolution=resolution, target=target, filters=filters,
        )

    recorder = TraceRecorder()

    try:
        result = estimate_yield(
            conn, target, filters, body.purchase_price_czk,
            trace_recorder=recorder,
        )
    except Exception as exc:
        LOG.warning("estimate_yield failed: %s", exc)
        error_msg = f"{type(exc).__name__}: {exc}"[:1000]
        return _persist_failed_run(
            conn, body=body, resolution=resolution, recorder=recorder,
            error_msg=error_msg, extra_warnings=[],
        )

    d = result["data"]
    summary_text = _summary_line(d, filters.radius_m)
    trace = recorder.to_dict(summary_text)
    merged_warnings = list(resolution.parse_warnings)
    merged_warnings.extend(d.get("warnings") or [])
    run_id = _insert_run(
        conn,
        source=body.source,
        mode=body.mode,
        status="success",
        input_url=resolution.input_url,
        input_sreality_id=resolution.input_sreality_id,
        input_spec=resolution.target_spec,
        input_purchase_price_czk=body.purchase_price_czk,
        estimated_monthly_rent_czk=d.get("estimated_monthly_rent_czk"),
        rent_p25_czk=d.get("rent_p25_czk"),
        rent_p75_czk=d.get("rent_p75_czk"),
        gross_yield_pct=d.get("gross_yield_pct"),
        confidence=d.get("confidence"),
        comparables_used=d.get("comparables_used"),
        trace=trace,
        warnings=merged_warnings or None,
        error_message=None,
        parent_run_id=body.parent_run_id,
        rerun_reason=body.rerun_reason,
        source_kind=resolution.source_kind,
        parse_confidence=resolution.parse_confidence,
        parse_confidence_per_field=resolution.parse_confidence_per_field,
        source_html=resolution.source_html,
    )
    return _fetch_run(conn, run_id) or {}


def get_estimation_run(
    conn: "psycopg.Connection", run_id: int
) -> dict[str, Any] | None:
    return _fetch_run(conn, run_id)


def list_estimation_runs(
    conn: "psycopg.Connection",
    *,
    source: str | None = None,
    status: str | None = None,
    sreality_id: int | None = None,
    source_kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    where: list[str] = []
    params: dict[str, Any] = {}
    if source is not None:
        where.append("er.source = %(source)s")
        params["source"] = source
    if status is not None:
        where.append("er.status = %(status)s")
        params["status"] = status
    if sreality_id is not None:
        where.append("er.input_sreality_id = %(sreality_id)s")
        params["sreality_id"] = sreality_id
    if source_kind is not None:
        where.append("er.source_kind = %(source_kind)s")
        params["source_kind"] = source_kind

    where_sql = "WHERE " + " AND ".join(where) if where else ""
    list_sql = (
        f"SELECT {_RUN_PROJECTION} FROM estimation_runs er {where_sql} "
        f"ORDER BY er.created_at DESC LIMIT %(limit)s OFFSET %(offset)s"
    )
    count_sql = f"SELECT count(*) FROM estimation_runs er {where_sql}"
    list_params = {**params, "limit": limit, "offset": offset}

    with conn.cursor() as cur:
        cur.execute(list_sql, list_params)
        rows = cur.fetchall()
        cur.execute(count_sql, params)
        total_row = cur.fetchone()
    total = int(total_row[0]) if total_row else 0
    return {
        "data": [_row_to_dict(_RUN_COLUMNS_OUT, r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


_LISTING_FIELDS: tuple[str, ...] = (
    "price_czk", "price_unit", "category_main", "category_type",
    "locality", "district", "locality_district_id", "locality_region_id",
    "total_floors", "has_balcony", "has_lift", "has_parking",
    "building_type", "condition", "energy_rating",
)


def _listing_from_result(
    result: "source_dispatcher.ParseResult",
) -> dict[str, Any]:
    if result.wide_spec is not None:
        return {f: result.wide_spec.get(f) for f in _LISTING_FIELDS}
    fx = result.full_extraction
    if fx is not None:
        out: dict[str, Any] = {}
        for f in _LISTING_FIELDS:
            env = fx.get(f)
            out[f] = env["value"] if isinstance(env, dict) and "value" in env else None
        return out
    return {f: None for f in _LISTING_FIELDS}


def preview_estimation(
    conn: "psycopg.Connection",
    sreality_client: "SrealityClient",
    llm_client: "LLMClient",
    body: s.PreviewEstimationIn,
) -> dict[str, Any]:
    """POST /estimations/preview: resolve URL, return parsed spec + provenance.

    Does NOT write to estimation_runs. Provenance fields (source_kind,
    parse_confidence, parse_confidence_per_field, from_cache, fetched_at,
    cost_usd, warnings) are returned to the caller so the UI can show what
    was extracted before the user commits to running the estimate.
    """
    result = source_dispatcher.parse_listing_url(
        body.url,
        sreality_client=sreality_client,
        llm_client=llm_client,
        conn=conn,
        force_refresh=body.force_refresh,
    )
    spec = dict(result.spec)
    if body.spec_overrides:
        spec = {**spec, **body.spec_overrides}
    return {
        "source_kind": result.source_kind,
        "parse_confidence": result.parse_confidence,
        "parse_confidence_per_field": result.parse_confidence_per_field,
        "spec": spec,
        "listing": _listing_from_result(result),
        "from_cache": result.from_cache,
        "fetched_at": result.fetched_at,
        "cost_usd": result.cost_usd,
        "warnings": list(result.warnings),
        "sreality_id": result.sreality_id,
        "source_url": result.source_url,
    }


# --- _resolve_input + helpers ---------------------------------------------

def _resolve_input(
    conn: "psycopg.Connection",
    sreality_client: "SrealityClient",
    llm_client: "LLMClient",
    body: s.CreateEstimationIn,
) -> _Resolution:
    """Build a _Resolution from the request body.

    URL path: dispatch through scraper.source_dispatcher (which routes
    sreality through the existing deterministic flow and any other
    domain through the LLM-driven per-source parser). Spec path: pass
    through with all parse-* fields None.
    """
    if body.url is not None:
        result = source_dispatcher.parse_listing_url(
            body.url,
            sreality_client=sreality_client,
            llm_client=llm_client,
            conn=conn,
        )
        spec = dict(result.spec)
        if body.spec_overrides:
            spec = {**spec, **body.spec_overrides}
        return _Resolution(
            input_url=body.url,
            input_sreality_id=result.sreality_id,
            target_spec=spec,
            source_kind=result.source_kind,
            parse_confidence=result.parse_confidence,
            parse_confidence_per_field=result.parse_confidence_per_field,
            source_html=result.source_html,
            parse_warnings=list(result.warnings),
        )
    assert body.spec is not None
    return _Resolution(
        input_url=None,
        input_sreality_id=None,
        target_spec=body.spec.model_dump(),
        source_kind=None,
        parse_confidence=None,
        parse_confidence_per_field=None,
        source_html=None,
        parse_warnings=[],
    )


def _resolution_for_parse_failure(
    body: s.CreateEstimationIn,
) -> _Resolution:
    """Best-effort _Resolution shape when the dispatcher itself raised.

    We have the URL but no parsed spec, no source_kind (we may have
    classified before the failure but we don't try to recover that
    here — failed runs are diagnostic, not a partial-success record).
    """
    return _Resolution(
        input_url=body.url,
        input_sreality_id=None,
        target_spec=None,
        source_kind=None,
        parse_confidence=None,
        parse_confidence_per_field=None,
        source_html=None,
        parse_warnings=[],
    )


def _persist_failed_run(
    conn: "psycopg.Connection",
    *,
    body: s.CreateEstimationIn,
    resolution: _Resolution,
    recorder: TraceRecorder,
    error_msg: str,
    extra_warnings: list[str],
) -> dict[str, Any]:
    trace = recorder.to_dict(f"failed: {error_msg.split(':', 1)[0]}")
    merged = list(resolution.parse_warnings) + list(extra_warnings or [])
    run_id = _insert_run(
        conn,
        source=body.source,
        mode=body.mode,
        status="failed",
        input_url=resolution.input_url,
        input_sreality_id=resolution.input_sreality_id,
        input_spec=resolution.target_spec,
        input_purchase_price_czk=body.purchase_price_czk,
        estimated_monthly_rent_czk=None,
        rent_p25_czk=None,
        rent_p75_czk=None,
        gross_yield_pct=None,
        confidence=None,
        comparables_used=None,
        trace=trace,
        warnings=merged or None,
        error_message=error_msg,
        parent_run_id=body.parent_run_id,
        rerun_reason=body.rerun_reason,
        source_kind=resolution.source_kind,
        parse_confidence=resolution.parse_confidence,
        parse_confidence_per_field=resolution.parse_confidence_per_field,
        source_html=resolution.source_html,
    )
    return _fetch_run(conn, run_id) or {}


def _build_target(spec: dict[str, Any] | None) -> TargetSpec:
    if spec is None:
        raise ValueError("target_spec is required to build a TargetSpec")
    return TargetSpec(
        lat=float(spec["lat"]),
        lng=float(spec["lng"]),
        area_m2=spec.get("area_m2"),
        disposition=spec.get("disposition"),
        floor=spec.get("floor"),
        exclude_ids=list(spec.get("exclude_ids") or []),
    )


def _build_filters(body: s.CreateEstimationIn) -> ComparableFilters:
    return ComparableFilters(
        radius_m=body.radius_m,
        area_band_pct=body.area_band_pct,
        disposition_match=body.disposition_match,
        max_age_days=body.max_age_days,
        active_only=body.active_only,
        floor_band=body.floor_band,
        condition_match=body.condition_match,
        building_type_match=body.building_type_match,
        energy_rating_match=body.energy_rating_match,
        has_balcony=body.has_balcony,
        has_lift=body.has_lift,
        has_parking=body.has_parking,
        min_price_czk=body.min_price_czk,
        max_price_czk=body.max_price_czk,
        category_main=body.category_main,
        category_type=body.category_type,
        locality_district_id=body.locality_district_id,
        locality_region_id=body.locality_region_id,
        include_unreliable=body.include_unreliable,
    )


def _summary_line(data: dict[str, Any], radius_m: int) -> str:
    n = data.get("sample_size") or 0
    confidence = data.get("confidence") or "unknown"
    rent = data.get("estimated_monthly_rent_czk")
    rent_part = f" estimated rent {rent} CZK/mo." if rent is not None else ""
    return (
        f"Found {n} comparables in {radius_m}m radius, "
        f"{confidence} confidence.{rent_part}"
    )


def _agent_summary_line(
    data: dict[str, Any], metadata: dict[str, Any],
) -> str:
    rent = data.get("estimated_monthly_rent_czk")
    confidence = data.get("confidence") or "unknown"
    iters = metadata.get("iterations") or 0
    stop = metadata.get("stop_reason") or "?"
    cost = metadata.get("total_cost_usd")
    cost_part = f" cost ${cost:.4f}" if isinstance(cost, (int, float)) else ""
    rent_part = (
        f" rent ~{rent} CZK/mo." if rent is not None else " no estimate."
    )
    return (
        f"agent {metadata.get('provider', '?')}/{metadata.get('skill', '?')} "
        f"after {iters} iter{'s' if iters != 1 else ''} ({stop}){cost_part}"
        f" {confidence}{rent_part}".strip()
    )


def _run_agent_path(
    conn: "psycopg.Connection",
    sreality_client: "SrealityClient",
    llm_client: "LLMClient",
    body: s.CreateEstimationIn,
    *,
    resolution: _Resolution,
    target: TargetSpec,
    filters: ComparableFilters,
) -> dict[str, Any]:
    """Agent-mode dispatch. Inserts a 'running' row, drives the agent
    loop, then UPDATEs to the terminal status. The early INSERT is what
    lets `llm_calls.estimation_run_id` attribute every per-turn call."""
    from api.agent import run_agent_estimation
    from api.skills import SkillNotFound, load_skill

    try:
        skill = load_skill(conn, body.skill)
    except SkillNotFound:
        recorder = TraceRecorder()
        return _persist_failed_run(
            conn, body=body, resolution=resolution, recorder=recorder,
            error_msg=f"unknown skill: {body.skill!r}",
            extra_warnings=[],
        )

    recorder = TraceRecorder()
    run_id = _insert_run(
        conn,
        source=body.source,
        mode="agent",
        status="running",
        input_url=resolution.input_url,
        input_sreality_id=resolution.input_sreality_id,
        input_spec=resolution.target_spec,
        input_purchase_price_czk=body.purchase_price_czk,
        estimated_monthly_rent_czk=None,
        rent_p25_czk=None,
        rent_p75_czk=None,
        gross_yield_pct=None,
        confidence=None,
        comparables_used=None,
        trace=recorder.to_dict("agent running"),
        warnings=None,
        error_message=None,
        parent_run_id=body.parent_run_id,
        rerun_reason=body.rerun_reason,
        source_kind=resolution.source_kind,
        parse_confidence=resolution.parse_confidence,
        parse_confidence_per_field=resolution.parse_confidence_per_field,
        source_html=resolution.source_html,
    )

    try:
        agent_result = run_agent_estimation(
            conn, sreality_client, llm_client,
            target, filters, body.purchase_price_czk,
            skill=skill, provider=body.provider,
            recorder=recorder, estimation_run_id=run_id,
        )
    except Exception as exc:
        LOG.warning("agent run failed: %s", exc)
        trace = recorder.to_dict(f"agent failed: {type(exc).__name__}")
        _update_run_terminal(
            conn, run_id,
            status="failed",
            trace=trace,
            warnings=list(resolution.parse_warnings) or None,
            error_message=f"{type(exc).__name__}: {exc}"[:1000],
        )
        return _fetch_run(conn, run_id) or {}

    d = agent_result.data
    md = agent_result.metadata
    status = "success" if md.get("stop_reason") == "record_estimate" else "failed"
    trace = recorder.to_dict(_agent_summary_line(d, md))
    merged_warnings = list(resolution.parse_warnings)
    merged_warnings.extend(d.get("warnings") or [])

    err: str | None = None
    if status == "failed":
        err = f"agent halted: {md.get('stop_reason')}"

    _update_run_terminal(
        conn, run_id,
        status=status,
        estimated_monthly_rent_czk=d.get("estimated_monthly_rent_czk"),
        rent_p25_czk=d.get("rent_p25_czk"),
        rent_p75_czk=d.get("rent_p75_czk"),
        gross_yield_pct=d.get("gross_yield_pct"),
        confidence=d.get("confidence"),
        comparables_used=d.get("comparables_used"),
        trace=trace,
        warnings=merged_warnings or None,
        error_message=err,
    )
    return _fetch_run(conn, run_id) or {}


def _update_run_terminal(
    conn: "psycopg.Connection",
    run_id: int,
    **fields: Any,
) -> None:
    """Parameterised UPDATE that writes only the supplied columns."""
    for k in ("comparables_used", "trace", "warnings"):
        if fields.get(k) is not None:
            fields[k] = Jsonb(fields[k])
    sets: list[str] = []
    params: dict[str, Any] = {"id": run_id}
    for col, val in fields.items():
        sets.append(f"{col} = %({col})s")
        params[col] = val
    sql = (
        f"UPDATE estimation_runs SET {', '.join(sets)} WHERE id = %(id)s"
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql, params)


def _insert_run(conn: "psycopg.Connection", **fields: Any) -> int:
    for k in (
        "input_spec", "comparables_used", "trace", "warnings",
        "parse_confidence_per_field",
    ):
        if fields.get(k) is not None:
            fields[k] = Jsonb(fields[k])
    cols = list(_INSERT_COLUMNS)
    cols_sql = ", ".join(cols)
    placeholders = ", ".join(f"%({c})s" for c in cols)
    sql = (
        f"INSERT INTO estimation_runs ({cols_sql}) "
        f"VALUES ({placeholders}) RETURNING id"
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql, fields)
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT did not return an id")
        return int(row[0])


def _fetch_run(
    conn: "psycopg.Connection", run_id: int
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_RUN_PROJECTION} FROM estimation_runs er WHERE er.id = %s",
            (run_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _row_to_dict(_RUN_COLUMNS_OUT, row)


def _row_to_dict(
    cols: tuple[str, ...] | list[str], row: tuple[Any, ...]
) -> dict[str, Any]:
    out: dict[str, Any] = dict(zip(cols, row))
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
    return out
