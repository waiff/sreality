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
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from api import schemas as s
from toolkit import ComparableFilters, TargetSpec

if TYPE_CHECKING:
    import psycopg

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
)

_INSERT_COLUMNS: tuple[str, ...] = tuple(
    c for c in _RUN_COLUMNS if c not in ("id", "created_at")
)


def create_estimation_run(
    conn: "psycopg.Connection",
    client: "SrealityClient",
    body: s.CreateEstimationIn,
) -> dict[str, Any]:
    """POST /estimations: resolve input, run the estimate, persist, return row.

    Synchronous deterministic mode goes straight to a terminal status —
    'success' or 'failed' — in a single INSERT. The schema reserves
    'pending'/'running' for U4's async agent without forcing today's
    code to UPDATE the row twice.
    """
    from api.estimate_yield import estimate_yield
    from scraper.url_parser import parse_sreality_url

    input_url, input_sreality_id, target_spec = _resolve_input(
        conn, client, body, parse_sreality_url
    )
    target = _build_target(target_spec)
    filters = _build_filters(body)
    recorder = TraceRecorder()

    try:
        result = estimate_yield(
            conn, target, filters, body.purchase_price_czk,
            trace_recorder=recorder,
        )
    except Exception as exc:
        LOG.warning("estimate_yield failed: %s", exc)
        error_msg = f"{type(exc).__name__}: {exc}"[:1000]
        trace = recorder.to_dict(f"failed: {type(exc).__name__}")
        run_id = _insert_run(
            conn,
            source=body.source,
            mode=body.mode,
            status="failed",
            input_url=input_url,
            input_sreality_id=input_sreality_id,
            input_spec=target_spec,
            input_purchase_price_czk=body.purchase_price_czk,
            estimated_monthly_rent_czk=None,
            rent_p25_czk=None,
            rent_p75_czk=None,
            gross_yield_pct=None,
            confidence=None,
            comparables_used=None,
            trace=trace,
            warnings=None,
            error_message=error_msg,
            parent_run_id=body.parent_run_id,
            rerun_reason=body.rerun_reason,
        )
        return _fetch_run(conn, run_id) or {}

    d = result["data"]
    summary_text = _summary_line(d, filters.radius_m)
    trace = recorder.to_dict(summary_text)
    run_id = _insert_run(
        conn,
        source=body.source,
        mode=body.mode,
        status="success",
        input_url=input_url,
        input_sreality_id=input_sreality_id,
        input_spec=target_spec,
        input_purchase_price_czk=body.purchase_price_czk,
        estimated_monthly_rent_czk=d.get("estimated_monthly_rent_czk"),
        rent_p25_czk=d.get("rent_p25_czk"),
        rent_p75_czk=d.get("rent_p75_czk"),
        gross_yield_pct=d.get("gross_yield_pct"),
        confidence=d.get("confidence"),
        comparables_used=d.get("comparables_used"),
        trace=trace,
        warnings=d.get("warnings"),
        error_message=None,
        parent_run_id=body.parent_run_id,
        rerun_reason=body.rerun_reason,
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
    cols_sql = ", ".join(_RUN_COLUMNS)
    list_sql = (
        f"SELECT {cols_sql} FROM estimation_runs {where_sql} "
        f"ORDER BY created_at DESC LIMIT %(limit)s OFFSET %(offset)s"
    )
    count_sql = f"SELECT count(*) FROM estimation_runs {where_sql}"
    list_params = {**params, "limit": limit, "offset": offset}

    with conn.cursor() as cur:
        cur.execute(list_sql, list_params)
        rows = cur.fetchall()
        cur.execute(count_sql, params)
        total_row = cur.fetchone()
    total = int(total_row[0]) if total_row else 0
    return {
        "data": [_row_to_dict(_RUN_COLUMNS, r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _resolve_input(
    conn: "psycopg.Connection",
    client: "SrealityClient",
    body: s.CreateEstimationIn,
    parse_sreality_url: Any,
) -> tuple[str | None, int | None, dict[str, Any]]:
    """Return (input_url, input_sreality_id, normalised_target_spec)."""
    if body.url is not None:
        parsed = parse_sreality_url(body.url, client=client, conn=conn)
        spec = _spec_from_parser(parsed["spec"])
        if body.spec_overrides:
            spec = {**spec, **body.spec_overrides}
        return body.url, int(parsed["sreality_id"]), spec
    assert body.spec is not None
    return None, None, body.spec.model_dump()


def _spec_from_parser(parser_spec: dict[str, Any]) -> dict[str, Any]:
    """Map parser.parse_listing output to TargetIn shape (lon → lng)."""
    return {
        "lat": parser_spec.get("lat"),
        "lng": parser_spec.get("lon"),
        "area_m2": parser_spec.get("area_m2"),
        "disposition": parser_spec.get("disposition"),
        "floor": parser_spec.get("floor"),
        "exclude_ids": [],
    }


def _build_target(spec: dict[str, Any]) -> TargetSpec:
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


def _insert_run(conn: "psycopg.Connection", **fields: Any) -> int:
    for k in ("input_spec", "comparables_used", "trace", "warnings"):
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
    cols_sql = ", ".join(_RUN_COLUMNS)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {cols_sql} FROM estimation_runs WHERE id = %s",
            (run_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _row_to_dict(_RUN_COLUMNS, row)


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
