"""Persistence and trace machinery for /estimations endpoints.

Trace contract (TRACE_SCHEMA_VERSION = 2):

    {
      "version": 2,
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
        },
        ...
      ]
    }

Version 2 is additive over version 1: agent-mode runs append a final
`computation` step labelled `'comparable_selection_summary'` whose
`output_summary` carries the per-iteration filter ladder + cohort
diffs + final picks. Deterministic-mode traces remain a 4-step flat
list; the only schema change at the deterministic level is the
`version` field.

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

TRACE_SCHEMA_VERSION = 2

# Filter defaults used by /estimations. Live values are read from
# `app_settings` (seeded by migration 052) so the operator can tune
# them via the Settings page without a redeploy. The constants below
# are fallbacks for when an app_settings row is missing or unreadable.
# Agent-mode runs use these as round-1 base filters; the agent
# overrides any of them per round through find_comparables_relaxed.
_DEFAULT_RADIUS_M = 1000
_DEFAULT_AREA_BAND_PCT = 0.20
_DEFAULT_DISPOSITION_MATCH = "exact"
_DEFAULT_ACTIVE_ONLY = True
_DEFAULT_MIN_RESULTS = 5


def _default_max_age_days(estimate_kind: str) -> int:
    return 7 if estimate_kind == "rent" else 30


def _load_app_setting(
    conn: "psycopg.Connection", key: str, fallback: Any,
) -> Any:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM app_settings WHERE key = %s", (key,),
            )
            row = cur.fetchone()
    except Exception as exc:
        LOG.warning("app_settings lookup failed for %r: %s", key, exc)
        return fallback
    if row is None or row[0] is None:
        return fallback
    return row[0]


@dataclass(frozen=True)
class FilterDefaults:
    radius_m: int
    area_band_pct: float
    disposition_match: str
    active_only: bool
    max_age_days_rent: int
    max_age_days_sale: int
    min_results: int

    def max_age_days_for(self, estimate_kind: str) -> int:
        if estimate_kind == "rent":
            return self.max_age_days_rent
        return self.max_age_days_sale


def load_filter_defaults(conn: "psycopg.Connection") -> FilterDefaults:
    return FilterDefaults(
        radius_m=int(_load_app_setting(conn, "default_radius_m", _DEFAULT_RADIUS_M)),
        area_band_pct=float(_load_app_setting(conn, "default_area_band_pct", _DEFAULT_AREA_BAND_PCT)),
        disposition_match=str(_load_app_setting(conn, "default_disposition_match", _DEFAULT_DISPOSITION_MATCH)),
        active_only=bool(_load_app_setting(conn, "default_active_only", _DEFAULT_ACTIVE_ONLY)),
        max_age_days_rent=int(_load_app_setting(conn, "default_max_age_days_rent", 7)),
        max_age_days_sale=int(_load_app_setting(conn, "default_max_age_days_sale", 30)),
        min_results=int(_load_app_setting(conn, "default_min_results", _DEFAULT_MIN_RESULTS)),
    )


class StepHandle:
    """Returned by recorder context managers; caller sets the summary."""

    _UNSET = object()

    def __init__(self) -> None:
        self.summary: dict[str, Any] = {}
        self.full_output: Any = StepHandle._UNSET

    def set_summary(self, summary: dict[str, Any]) -> None:
        self.summary = summary

    def set_full_output(self, full_output: Any) -> None:
        """Capture the unbounded tool output for the trace_payloads side-table.

        Trace JSONB stores only `output_summary` per architectural rule #9;
        full payloads land in `estimation_trace_payloads` keyed on
        (estimation_run_id, step_n) for click-to-expand drill-down.
        """
        self.full_output = full_output


class TraceRecorder:
    """Captures tool calls and computations into the trace format."""

    def __init__(self) -> None:
        self._steps: list[dict[str, Any]] = []
        self._payloads: list[tuple[int, Any]] = []
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

    def iter_payloads(self) -> list[tuple[int, Any]]:
        """Return the (step_n, full_output) pairs captured during the run."""
        return list(self._payloads)

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
        if handle.full_output is not StepHandle._UNSET:
            self._payloads.append((self._n, handle.full_output))


class _NullStepHandle:
    def set_summary(self, summary: dict[str, Any]) -> None:
        return None

    def set_full_output(self, full_output: Any) -> None:
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

    def iter_payloads(self) -> list[tuple[int, Any]]:
        return []


_NULL_HANDLE = _NullStepHandle()
NULL_RECORDER: Any = _NullTraceRecorder()


def flush_trace_payloads(
    conn: "psycopg.Connection",
    run_id: int,
    recorder: TraceRecorder,
) -> None:
    """Persist the recorder's accumulated tool-call full outputs.

    Called after the parent estimation_runs row exists. ON CONFLICT
    DO NOTHING so a retry path that double-flushes is a no-op.

    Best-effort: a failure here must never bubble up. The run row is
    already committed at the call site; losing the drill-down side-
    table for a step is a UX degradation, not a request failure.
    """
    rows = recorder.iter_payloads()
    if not rows:
        return
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO estimation_trace_payloads "
                "(estimation_run_id, step_n, full_output) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (estimation_run_id, step_n) DO NOTHING",
                [(run_id, step_n, Jsonb(payload)) for step_n, payload in rows],
            )
    except Exception as exc:
        LOG.warning(
            "flush_trace_payloads failed for run %s: %s: %s",
            run_id, type(exc).__name__, exc,
        )


def _ms_since(mono_start: float) -> int:
    return int((time.monotonic() - mono_start) * 1000)


# --- create / get / list endpoints -----------------------------------------

_RUN_COLUMNS: tuple[str, ...] = (
    "id", "created_at", "source", "mode", "status",
    "estimate_kind",
    "input_url", "input_sreality_id", "input_spec",
    "input_purchase_price_czk",
    "estimated_monthly_rent_czk", "rent_p25_czk", "rent_p75_czk",
    "estimated_sale_price_czk", "sale_p25_czk", "sale_p75_czk",
    "gross_yield_pct", "confidence",
    "comparables_used", "comparables_excluded",
    "trace", "warnings", "error_message",
    "parent_run_id", "rerun_reason",
    "source_kind", "parse_confidence", "parse_confidence_per_field",
    "source_html",
    "subject_summary",
    "special_instructions", "contextual_text",
    "skill_name", "skill_version",
    "scenario",
)

_INSERT_COLUMNS: tuple[str, ...] = tuple(
    c for c in _RUN_COLUMNS if c not in ("id", "created_at")
)

_COST_TOTAL_SUBSELECT = (
    "coalesce("
    "(SELECT sum(cost_usd) FROM llm_calls WHERE estimation_run_id = er.id), "
    "0)::float AS cost_usd_total"
)
# Boolean: is there at least one operator-supplied feedback row on
# this run? Drives the "Feedback" button enable/disable on the
# /estimations list (slice B follow-up).
_HAS_FEEDBACK_SUBSELECT = (
    "EXISTS("
    "SELECT 1 FROM estimation_feedback WHERE estimation_run_id = er.id"
    ") AS has_feedback"
)
# Best-available city/locality string for the /estimations list:
# - sreality runs use listings.district ("Praha 2"-style) via LEFT JOIN
# - non-sreality runs fall back to the locality the LLM parser stored
#   in parsed_url_cache.parse_result.extraction.locality.value
# Scalar subquery (not a join) on parsed_url_cache since source_url
# isn't unique there — pick the freshest row.
_LOCALITY_DISPLAY_EXPR = (
    "coalesce("
    "l.district, "
    "(SELECT puc.parse_result->'extraction'->'locality'->>'value' "
    "FROM parsed_url_cache puc "
    "WHERE puc.source_url = er.input_url "
    "ORDER BY puc.parsed_at DESC LIMIT 1)"
    ") AS locality_display"
)
_RUN_PROJECTION = (
    ", ".join(f"er.{c}" for c in _RUN_COLUMNS)
    + ", " + _COST_TOTAL_SUBSELECT
    + ", " + _HAS_FEEDBACK_SUBSELECT
)
_LIST_PROJECTION = _RUN_PROJECTION + ", " + _LOCALITY_DISPLAY_EXPR
_LIST_FROM = (
    "estimation_runs er "
    "LEFT JOIN listings l ON l.sreality_id = er.input_sreality_id"
)
_RUN_COLUMNS_OUT: tuple[str, ...] = _RUN_COLUMNS + (
    "cost_usd_total", "has_feedback",
)
_LIST_COLUMNS_OUT: tuple[str, ...] = _RUN_COLUMNS_OUT + ("locality_display",)


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


def _build_subject_summary(
    conn: "psycopg.Connection",
    llm_client: "LLMClient",
    sreality_id: int | None,
) -> dict[str, Any] | None:
    """Run summarize_listing on the subject, best-effort.

    Subject summary is informational; a failure (no listing in DB, LLM
    refusal, missing API key) must not turn a successful estimation into
    a failed one. Return None and let the caller persist that.
    """
    if sreality_id is None:
        return None
    try:
        from toolkit.summaries import summarize_listing
        result = summarize_listing(conn, llm_client, sreality_id=sreality_id)
    except Exception as exc:  # noqa: BLE001 — see docstring
        LOG.info("subject summary skipped for %s: %s", sreality_id, exc)
        return None
    data = result.get("data") or {}
    summary = data.get("summary")
    if not isinstance(summary, dict):
        return None
    return {
        "snapshot_id": data.get("snapshot_id"),
        "summary": summary,
    }


def create_estimation_run(
    conn: "psycopg.Connection",
    sreality_client: "SrealityClient",
    llm_client: "LLMClient",
    body: s.CreateEstimationIn,
    background_tasks: Any | None = None,
) -> dict[str, Any]:
    """POST /estimations: parse the URL, INSERT a pending row, schedule the
    heavy work as a BackgroundTask, return the row immediately.

    The handler completes in ~1 s (just URL parse + INSERT) instead of
    waiting 3–10 s for the full estimate. The browser navigates to the
    detail page, which polls until the row reaches a terminal status.

    Hard failures during setup (URL parse, target / filter build, skill
    lookup) are still persisted inline with `status='failed'` — those
    rows have no work to background.

    When `background_tasks` is None (tests that want synchronous
    behaviour, ClickUp / agent callers that want the row populated
    before they read it back), the heavy work runs inline on the
    request thread.
    """
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

    try:
        target = _build_target(resolution.target_spec, resolution.input_sreality_id)
        filters = _build_filters(body, load_filter_defaults(conn))
    except Exception as exc:
        LOG.warning("target/filters build failed: %s", exc)
        return _persist_failed_run(
            conn, body=body, resolution=resolution, recorder=TraceRecorder(),
            error_msg=f"target build failed: {type(exc).__name__}: {exc}"[:1000],
            extra_warnings=[],
        )

    skill_obj = None
    if body.mode == "agent":
        from api.skills import SkillNotFound, load_skill
        try:
            skill_obj = load_skill(conn, body.skill)
        except SkillNotFound:
            return _persist_failed_run(
                conn, body=body, resolution=resolution,
                recorder=TraceRecorder(),
                error_msg=f"unknown skill: {body.skill!r}",
                extra_warnings=[],
            )
        initial_status = "running"
    else:
        initial_status = "pending"

    run_id = _insert_run(
        conn,
        source=body.source,
        mode=body.mode,
        status=initial_status,
        estimate_kind=body.estimate_kind,
        input_url=resolution.input_url,
        input_sreality_id=resolution.input_sreality_id,
        input_spec=resolution.target_spec,
        input_purchase_price_czk=body.purchase_price_czk,
        estimated_monthly_rent_czk=None,
        rent_p25_czk=None,
        rent_p75_czk=None,
        estimated_sale_price_czk=None,
        sale_p25_czk=None,
        sale_p75_czk=None,
        gross_yield_pct=None,
        confidence=None,
        comparables_used=None,
        comparables_excluded=None,
        trace=TraceRecorder().to_dict("pending"),
        warnings=list(resolution.parse_warnings) or None,
        error_message=None,
        parent_run_id=body.parent_run_id,
        rerun_reason=body.rerun_reason,
        source_kind=resolution.source_kind,
        parse_confidence=resolution.parse_confidence,
        parse_confidence_per_field=resolution.parse_confidence_per_field,
        source_html=resolution.source_html,
        subject_summary=None,
        special_instructions=body.special_instructions,
        contextual_text=body.contextual_text,
        skill_name=skill_obj.name if skill_obj is not None else None,
        skill_version=skill_obj.version if skill_obj is not None else None,
    )

    if background_tasks is not None:
        background_tasks.add_task(
            _execute_estimation_run_background,
            run_id=run_id,
            body=body,
            resolution=resolution,
        )
        return _fetch_run(conn, run_id) or {}

    _execute_estimation_run(
        conn, sreality_client, llm_client, run_id,
        body=body, resolution=resolution, target=target, filters=filters,
    )
    return _fetch_run(conn, run_id) or {}


def _execute_estimation_run_background(
    *,
    run_id: int,
    body: s.CreateEstimationIn,
    resolution: _Resolution,
) -> None:
    """Background-task entry: opens its own connection + clients and
    runs the heavy work. Any uncaught exception flips the row to
    'failed' so it can't get stuck.
    """
    from api import dependencies as deps

    try:
        with deps.open_background_conn() as conn:
            from api.llm_client import LLMClient
            sreality_client = deps.get_sreality_client()
            llm_client = LLMClient(conn, providers=deps.get_providers())
            try:
                target = _build_target(
                    resolution.target_spec, resolution.input_sreality_id,
                )
                filters = _build_filters(body, load_filter_defaults(conn))
            except Exception as exc:
                LOG.exception("background target/filters build failed for run %s", run_id)
                _safe_mark_failed(
                    conn, run_id,
                    f"target build failed: {type(exc).__name__}: {exc}"[:1000],
                )
                return
            _execute_estimation_run(
                conn, sreality_client, llm_client, run_id,
                body=body, resolution=resolution,
                target=target, filters=filters,
            )
    except Exception as exc:
        LOG.exception("background estimation run %s crashed", run_id)
        # Last-ditch: open a fresh connection so we can still mark failed.
        try:
            with deps.open_background_conn() as conn:
                _safe_mark_failed(
                    conn, run_id,
                    f"background crash: {type(exc).__name__}: {exc}"[:1000],
                )
        except Exception:
            LOG.exception(
                "failed to mark run %s failed after background crash", run_id,
            )


def _safe_mark_failed(
    conn: "psycopg.Connection", run_id: int, error_msg: str,
) -> None:
    """UPDATE the row to status='failed' with an error_message.

    Used as a last-ditch path when the background task can't otherwise
    record the failure (e.g. it crashed before the per-step trace was
    finalised). Best-effort — never raises.
    """
    try:
        _update_run_terminal(
            conn, run_id, status="failed", error_message=error_msg,
        )
    except Exception:
        LOG.exception("_safe_mark_failed failed for run %s", run_id)


def _execute_estimation_run(
    conn: "psycopg.Connection",
    sreality_client: "SrealityClient",
    llm_client: "LLMClient",
    run_id: int,
    *,
    body: s.CreateEstimationIn,
    resolution: _Resolution,
    target: TargetSpec,
    filters: ComparableFilters,
) -> None:
    """Run the heavy estimation work for an already-INSERTed row.

    Deterministic mode: runs estimate_yield, UPDATEs to terminal.
    Agent mode: delegates to the existing agent dispatch which UPDATEs
    the same row in place.
    """
    if body.mode == "agent":
        _run_agent_path(
            conn, sreality_client, llm_client, run_id, body,
            resolution=resolution, target=target, filters=filters,
        )
        return

    from api.estimate_yield import estimate_yield

    recorder = TraceRecorder()
    try:
        result = estimate_yield(
            conn, target, filters, body.purchase_price_czk,
            estimate_kind=body.estimate_kind,
            expected_monthly_rent_czk=body.expected_monthly_rent_czk,
            trace_recorder=recorder,
        )
    except Exception as exc:
        LOG.warning("estimate_yield failed for run %s: %s", run_id, exc)
        error_msg = f"{type(exc).__name__}: {exc}"[:1000]
        trace = recorder.to_dict(f"failed: {error_msg.split(':', 1)[0]}")
        _update_run_terminal(
            conn, run_id,
            status="failed",
            trace=trace,
            warnings=list(resolution.parse_warnings) or None,
            error_message=error_msg,
        )
        flush_trace_payloads(conn, run_id, recorder)
        return

    d = result["data"]
    summary_text = _summary_line(d, filters.radius_m)
    trace = recorder.to_dict(summary_text)
    merged_warnings = list(resolution.parse_warnings)
    merged_warnings.extend(d.get("warnings") or [])
    subject_summary = _build_subject_summary(
        conn, llm_client, resolution.input_sreality_id,
    )
    _update_run_terminal(
        conn, run_id,
        status="success",
        estimated_monthly_rent_czk=d.get("estimated_monthly_rent_czk"),
        rent_p25_czk=d.get("rent_p25_czk"),
        rent_p75_czk=d.get("rent_p75_czk"),
        estimated_sale_price_czk=d.get("estimated_sale_price_czk"),
        sale_p25_czk=d.get("sale_p25_czk"),
        sale_p75_czk=d.get("sale_p75_czk"),
        gross_yield_pct=d.get("gross_yield_pct"),
        confidence=d.get("confidence"),
        comparables_used=d.get("comparables_used"),
        trace=trace,
        warnings=merged_warnings or None,
        subject_summary=subject_summary,
    )
    flush_trace_payloads(conn, run_id, recorder)


def get_estimation_run(
    conn: "psycopg.Connection", run_id: int
) -> dict[str, Any] | None:
    return _fetch_run(conn, run_id)


def update_scenario(
    conn: "psycopg.Connection",
    run_id: int,
    *,
    rent_czk: float | None,
    fond_per_m2_czk: float | None,
    price_czk: float | None,
) -> dict[str, Any] | None:
    """PATCH the operator-tunable yield scenario on an estimation_runs row.

    A body with all three numbers None clears the column back to NULL
    (re-render defaults). Otherwise we store the supplied subset plus
    an `updated_at` stamp so concurrent edits between the SPA and the
    Chrome extension can be reasoned about.

    Returns the refreshed row, or None when the run id is unknown.
    """
    has_any = any(
        v is not None for v in (rent_czk, fond_per_m2_czk, price_czk)
    )
    if has_any:
        payload: dict[str, Any] = {
            "rent_czk": rent_czk,
            "fond_per_m2_czk": fond_per_m2_czk,
            "price_czk": price_czk,
            "updated_at": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds",
            ),
        }
        scenario_value: Any = Jsonb(payload)
    else:
        scenario_value = None
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE estimation_runs SET scenario = %s "
            "WHERE id = %s RETURNING id",
            (scenario_value, run_id),
        )
        if cur.fetchone() is None:
            return None
    return _fetch_run(conn, run_id)


def sweep_stuck_runs(
    conn: "psycopg.Connection",
    *,
    older_than_minutes: int = 10,
) -> int:
    """Mark any estimation_runs in a non-terminal status older than the
    cutoff as 'failed'. Returns the number of rows updated.

    Called from the FastAPI lifespan startup hook to recover rows
    orphaned by a server restart mid-background-task. Manual SQL is
    fine for one-off cleanup; this is the routine path so the operator
    doesn't have to.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE estimation_runs "
            "SET status = 'failed', "
            "    error_message = coalesce(error_message, "
            "        'interrupted by server restart') "
            "WHERE status IN ('pending', 'running') "
            "  AND created_at < now() - make_interval(mins => %s) "
            "RETURNING id",
            (older_than_minutes,),
        )
        return len(cur.fetchall())


def get_trace_payload(
    conn: "psycopg.Connection", run_id: int, step_n: int,
) -> dict[str, Any] | None:
    """Fetch one estimation_trace_payloads row, or None if absent.

    Returns `{step_n, full_output, captured_at}`. Drives the
    click-to-expand drill-down on tool_call steps in the timeline.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT step_n, full_output, captured_at "
            "FROM estimation_trace_payloads "
            "WHERE estimation_run_id = %s AND step_n = %s",
            (run_id, step_n),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "step_n": row[0],
        "full_output": row[1],
        "captured_at": (
            row[2].isoformat(timespec="milliseconds")
            if row[2] is not None
            else None
        ),
    }


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
        f"SELECT {_LIST_PROJECTION} FROM {_LIST_FROM} {where_sql} "
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
        "data": [_row_to_dict(_LIST_COLUMNS_OUT, r) for r in rows],
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
        estimate_kind=body.estimate_kind,
        input_url=resolution.input_url,
        input_sreality_id=resolution.input_sreality_id,
        input_spec=resolution.target_spec,
        input_purchase_price_czk=body.purchase_price_czk,
        estimated_monthly_rent_czk=None,
        rent_p25_czk=None,
        rent_p75_czk=None,
        estimated_sale_price_czk=None,
        sale_p25_czk=None,
        sale_p75_czk=None,
        gross_yield_pct=None,
        confidence=None,
        comparables_used=None,
        comparables_excluded=None,
        trace=trace,
        warnings=merged or None,
        error_message=error_msg,
        parent_run_id=body.parent_run_id,
        rerun_reason=body.rerun_reason,
        source_kind=resolution.source_kind,
        parse_confidence=resolution.parse_confidence,
        parse_confidence_per_field=resolution.parse_confidence_per_field,
        source_html=resolution.source_html,
        subject_summary=None,
        special_instructions=body.special_instructions,
        contextual_text=body.contextual_text,
        skill_name=None,
        skill_version=None,
    )
    flush_trace_payloads(conn, run_id, recorder)
    return _fetch_run(conn, run_id) or {}


def _load_subject_condition(
    conn: "psycopg.Connection",
    sreality_id: int | None,
) -> dict[str, Any] | None:
    """Fetch the subject listing's condition fields for the agent prompt.

    Returns the sreality `condition` enum plus the two derived 1-5
    levels (apartment + building). Derived levels are NULL until the
    scoring phase has run on that snapshot; the agent prompt teaches
    the LLM to skip the matching level_min filter when its target is
    NULL on that axis.
    """
    if sreality_id is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT condition, apartment_condition_level, "
            "building_condition_level FROM listings WHERE sreality_id = %s",
            (sreality_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "condition": row[0],
        "apartment_condition_level": row[1],
        "building_condition_level": row[2],
    }


def _build_target(
    spec: dict[str, Any] | None,
    input_sreality_id: int | None = None,
) -> TargetSpec:
    if spec is None:
        raise ValueError("target_spec is required to build a TargetSpec")
    exclude_ids = list(spec.get("exclude_ids") or [])
    if input_sreality_id is not None and input_sreality_id not in exclude_ids:
        exclude_ids.append(int(input_sreality_id))
    return TargetSpec(
        lat=float(spec["lat"]),
        lng=float(spec["lng"]),
        area_m2=spec.get("area_m2"),
        disposition=spec.get("disposition"),
        floor=spec.get("floor"),
        exclude_ids=exclude_ids,
    )


def _build_filters(
    body: s.CreateEstimationIn,
    defaults: FilterDefaults,
) -> ComparableFilters:
    return ComparableFilters(
        radius_m=defaults.radius_m,
        area_band_pct=defaults.area_band_pct,
        disposition_match=defaults.disposition_match,
        max_age_days=defaults.max_age_days_for(body.estimate_kind),
        active_only=defaults.active_only,
        population=body.population,
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
        category_sub_cb=body.category_sub_cb,
        furnished=body.furnished,
        terrace=body.terrace,
        cellar=body.cellar,
        garage=body.garage,
        ownership=body.ownership,
        min_estate_area=body.min_estate_area,
        max_estate_area=body.max_estate_area,
        min_usable_area=body.min_usable_area,
        max_usable_area=body.max_usable_area,
        min_parking_lots=body.min_parking_lots,
        building_condition_level_min=body.building_condition_level_min,
        apartment_condition_level_min=body.apartment_condition_level_min,
        tom_days_min=body.tom_days_min,
        tom_days_max=body.tom_days_max,
        last_seen_min_days=body.last_seen_min_days,
        last_seen_max_days=body.last_seen_max_days,
        first_seen_min_days=body.first_seen_min_days,
        first_seen_max_days=body.first_seen_max_days,
    )


def _summary_line(data: dict[str, Any], radius_m: int) -> str:
    n = data.get("sample_size") or 0
    confidence = data.get("confidence") or "unknown"
    kind = data.get("estimate_kind") or "rent"
    if kind == "sale":
        point = data.get("estimated_sale_price_czk")
        point_part = (
            f" estimated sale price {point} CZK." if point is not None else ""
        )
    else:
        point = data.get("estimated_monthly_rent_czk")
        point_part = (
            f" estimated rent {point} CZK/mo." if point is not None else ""
        )
    return (
        f"Found {n} comparables in {radius_m}m radius, "
        f"{confidence} confidence.{point_part}"
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
        f"after {iters} LLM turn{'s' if iters != 1 else ''} ({stop}){cost_part}"
        f" {confidence}{rent_part}".strip()
    )


def _run_agent_path(
    conn: "psycopg.Connection",
    sreality_client: "SrealityClient",
    llm_client: "LLMClient",
    run_id: int,
    body: s.CreateEstimationIn,
    *,
    resolution: _Resolution,
    target: TargetSpec,
    filters: ComparableFilters,
) -> None:
    """Agent-mode dispatch. The row is already INSERTed with
    status='running' by the caller; this drives the agent loop and
    UPDATEs to a terminal status. The early INSERT is what lets
    `llm_calls.estimation_run_id` attribute every per-turn call."""
    from api.agent import run_agent_estimation
    from api.skills import load_skill

    # Skill existence is validated by the caller before INSERT — we
    # re-load here because the background path opens a fresh connection.
    skill = load_skill(conn, body.skill)

    recorder = TraceRecorder()

    try:
        agent_result = run_agent_estimation(
            conn, sreality_client, llm_client,
            target, filters, body.purchase_price_czk,
            skill=skill, provider=body.provider,
            recorder=recorder, estimation_run_id=run_id,
            special_instructions=body.special_instructions,
            contextual_text=body.contextual_text,
            subject_condition=_load_subject_condition(
                conn, resolution.input_sreality_id,
            ),
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
        flush_trace_payloads(conn, run_id, recorder)
        return

    d = agent_result.data
    md = agent_result.metadata
    status = "success" if md.get("stop_reason") == "record_estimate" else "failed"
    trace = recorder.to_dict(_agent_summary_line(d, md))
    merged_warnings = list(resolution.parse_warnings)
    merged_warnings.extend(d.get("warnings") or [])

    err: str | None = None
    if status == "failed":
        err = f"agent halted: {md.get('stop_reason')}"

    subject_summary = (
        _build_subject_summary(conn, llm_client, resolution.input_sreality_id)
        if status == "success"
        else None
    )

    _update_run_terminal(
        conn, run_id,
        status=status,
        estimated_monthly_rent_czk=d.get("estimated_monthly_rent_czk"),
        rent_p25_czk=d.get("rent_p25_czk"),
        rent_p75_czk=d.get("rent_p75_czk"),
        gross_yield_pct=d.get("gross_yield_pct"),
        confidence=d.get("confidence"),
        comparables_used=d.get("comparables_used"),
        comparables_excluded=d.get("comparables_excluded") or None,
        trace=trace,
        warnings=merged_warnings or None,
        error_message=err,
        subject_summary=subject_summary,
    )
    flush_trace_payloads(conn, run_id, recorder)


def _update_run_terminal(
    conn: "psycopg.Connection",
    run_id: int,
    **fields: Any,
) -> None:
    """Parameterised UPDATE that writes only the supplied columns."""
    for k in (
        "comparables_used", "comparables_excluded",
        "trace", "warnings", "subject_summary",
    ):
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
    for col in _INSERT_COLUMNS:
        fields.setdefault(col, None)
    for k in (
        "input_spec", "comparables_used", "comparables_excluded",
        "trace", "warnings",
        "parse_confidence_per_field", "subject_summary",
        "scenario",
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
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_RUN_PROJECTION} FROM estimation_runs er WHERE er.id = %s",
                (run_id,),
            )
            row = cur.fetchone()
    except Exception as exc:
        LOG.warning(
            "_fetch_run failed for run %s: %s: %s",
            run_id, type(exc).__name__, exc,
        )
        return {"id": run_id, "status": "success"}
    if row is None:
        return None
    try:
        return _row_to_dict(_RUN_COLUMNS_OUT, row)
    except Exception as exc:
        LOG.warning(
            "_row_to_dict failed for run %s: %s: %s",
            run_id, type(exc).__name__, exc,
        )
        return {"id": run_id, "status": "success"}


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
