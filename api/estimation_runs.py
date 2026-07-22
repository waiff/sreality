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
import hashlib
import json
import logging
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import HTTPException

from api.cursor import decode_cursor, encode_cursor
from api.dependencies import SYSTEM_ACCOUNT_ID

from psycopg.types.json import Jsonb

from api import schemas as s
from scraper import source_dispatcher
from toolkit import ComparableFilters, TargetSpec
from toolkit.rent_map import compute_reference_rent

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
_DEFAULT_LIFECYCLE = "active"
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
    lifecycle: str
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
        lifecycle=str(_load_app_setting(conn, "default_lifecycle", _DEFAULT_LIFECYCLE)),
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
    "id", "created_at", "account_id", "source", "mode", "status",
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
    "subject_attributes",
    "special_instructions", "contextual_text",
    "skill_name", "skill_version",
    "scenario",
    "reference_rent",
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
# List rows omit source_html (raw page bytes, LLM path only — large and never
# rendered in a list context; the detail endpoint still returns it).
_LIST_COLUMNS: tuple[str, ...] = tuple(
    c for c in _RUN_COLUMNS if c != "source_html"
)
_LIST_PROJECTION = (
    ", ".join(f"er.{c}" for c in _LIST_COLUMNS)
    + ", " + _COST_TOTAL_SUBSELECT
    + ", " + _HAS_FEEDBACK_SUBSELECT
    + ", " + _LOCALITY_DISPLAY_EXPR
)
_LIST_FROM = (
    "estimation_runs er "
    "LEFT JOIN listings l ON l.sreality_id = er.input_sreality_id"
)
_RUN_COLUMNS_OUT: tuple[str, ...] = _RUN_COLUMNS + (
    "cost_usd_total", "has_feedback",
)
_LIST_COLUMNS_OUT: tuple[str, ...] = _LIST_COLUMNS + (
    "cost_usd_total", "has_feedback", "locality_display",
)


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
    subject_listing_price_czk: int | None = None
    subject_listing_category_type: str | None = None
    yield_input_derivation: dict[str, Any] | None = None
    # Typed subject attributes (building_type / condition / amenities / …) for a
    # subject with no resolved listings row — lets the UI render the subject like
    # a listing. None when input_sreality_id is set (the UI reads listings_public).
    subject_attributes: dict[str, Any] | None = None


_EMPTY_RESOLUTION = _Resolution(
    input_url=None, input_sreality_id=None, target_spec=None,
    source_kind=None, parse_confidence=None,
    parse_confidence_per_field=None, source_html=None,
    parse_warnings=[],
)

JOB_LANE_SETTING = "estimation_job_lane_enabled"


def _job_lane_enabled(conn: "psycopg.Connection") -> bool:
    """True when POST /estimations should hand execution to the realtime
    worker's estimation lane (Amendment A10) instead of running the heavy work
    in-process. Reads app_settings.estimation_job_lane_enabled — an absent key
    (or any read error) means False, so the lane ships dark and a settings
    hiccup never strands a submit."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM app_settings WHERE key = %s",
                (JOB_LANE_SETTING,),
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 - a settings read must never fail a submit
        LOG.warning("job-lane flag read failed; treating as disabled")
        return False
    if row is None:
        return False
    value = row[0]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def _job_payload(
    body: s.CreateEstimationIn, resolution: _Resolution,
) -> dict[str, Any]:
    """Snapshot the execution inputs for the worker job lane. The run row IS the
    job (no new table): {body, resolution} is everything _execute_estimation_run
    needs, captured AFTER yield-input derivation so the worker re-runs nothing.
    source_html is dropped — it's already persisted as its own column and the
    execution path never reads resolution.source_html, so this keeps the
    (transient) payload small."""
    res = asdict(resolution)
    res["source_html"] = None
    return {"body": body.model_dump(mode="json"), "resolution": res}


def execute_pending_run(
    conn: "psycopg.Connection",
    sreality_client: "SrealityClient",
    llm_client: "LLMClient",
    run_id: int,
    payload: dict[str, Any],
) -> None:
    """Execute a `pending` estimation_runs row claimed by the realtime worker's
    estimation lane. Rehydrates the job_payload snapshot, rebuilds target/filters,
    and runs the SAME heavy path the in-process BackgroundTask runs — only the
    executor moved off the request threadpool (Amendment A10). Fully
    self-contained: any failure flips the row to 'failed', so a claimed row can
    never get stuck. Never raises."""
    try:
        body = s.CreateEstimationIn(**payload["body"])
        resolution = _Resolution(**payload["resolution"])
    except Exception as exc:  # noqa: BLE001
        LOG.exception("estimation lane: invalid job_payload for run %s", run_id)
        _safe_mark_failed(
            conn, run_id,
            f"job payload invalid: {type(exc).__name__}: {exc}"[:1000],
        )
        return
    try:
        target = _build_target(
            resolution.target_spec, resolution.input_sreality_id,
        )
        filters = _build_filters(body, load_filter_defaults(conn))
    except Exception as exc:  # noqa: BLE001
        LOG.exception(
            "estimation lane: target/filters build failed for run %s", run_id,
        )
        _safe_mark_failed(
            conn, run_id,
            f"target build failed: {type(exc).__name__}: {exc}"[:1000],
        )
        return
    try:
        _execute_estimation_run(
            conn, sreality_client, llm_client, run_id,
            body=body, resolution=resolution, target=target, filters=filters,
        )
    except Exception as exc:  # noqa: BLE001 - the lane must not crash on one run
        LOG.exception("estimation lane: run %s crashed", run_id)
        _safe_mark_failed(
            conn, run_id, f"lane crash: {type(exc).__name__}: {exc}"[:1000],
        )


# --- Wave 1 metering + atomic submit-time gates (Phase 1 items J + A9) --------
#
# The paid agent estimation is metered per SUCCESSFUL run against a MONTHLY quota
# (operator decision 2026-07-22 — run-count, not USD; free = 3/mo, trial = 10).
# Deterministic runs stay free + ungated. Only a real, non-admin tenant sending
# mode='agent' is metered; admin / legacy-static-token / SYSTEM callers (operator,
# ClickUp, tests) bypass everything, exactly like require_entitlement.
#
# The spine is atomic (A9 — check-then-act is TOCTOU-racy over the tx pooler, the
# mig-279 lesson): idempotency + single-in-flight ride a UNIQUE partial index +
# ON CONFLICT (mig 355); the monthly budget + concurrency cap ride an atomic
# INSERT ... SELECT WHERE (count) < limit. A cheap PRE-parse check short-circuits
# the common duplicate / over-quota case before the URL parse spends an LLM call
# (parse cost can't attribute to a not-yet-existing run — llm_client stamps
# estimation_run_id=NULL — so only a pre-parse gate caps it).

AGENT_ESTIMATION_ACTION = "agent_estimation"
BUDGET_ENABLED_SETTING = "estimation_budget_enabled"   # absent => enabled (fail-closed)
CONCURRENCY_CAP_SETTING = "agent_estimation_concurrency_cap"
CONCURRENCY_CAP_DEFAULT = 3
_TRACKING_QUERY_PREFIXES = ("utm_",)
_TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "gbraid", "wbraid", "mc_eid", "igshid", "ref", "src",
}


@dataclass
class _MeterDecision:
    idempotency_key: str
    quota: int
    concurrency_cap: int
    short_circuit_run: dict[str, Any] | None = None


def _is_privileged(claims: dict[str, Any] | None) -> bool:
    """Admin / legacy-static-token callers bypass metering (operator, ClickUp,
    internal + tests), mirroring require_entitlement's bypass."""
    if claims is None:
        return True   # internal caller (ClickUp / agent / test) — never metered
    if claims.get("legacy") or claims.get("is_admin") is True:
        return True
    meta = claims.get("app_metadata") or {}
    return meta.get("is_admin") is True


def _budget_enabled(conn: "psycopg.Connection") -> bool:
    val = _load_app_setting(conn, BUDGET_ENABLED_SETTING, True)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes", "on")
    return bool(val)


def _canonical_url(url: str) -> str:
    """Canonicalize a portal URL for the idempotency key: https scheme, lower
    host, no fragment, tracking params dropped, remaining query sorted, no
    trailing slash — so query-param / scheme / trailing-slash variance across
    portals doesn't double-charge the same listing."""
    try:
        parts = urlsplit(url.strip())
    except Exception:  # noqa: BLE001 - a weird URL is still a usable raw key
        return url.strip()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    q = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (
            k.lower() in _TRACKING_QUERY_KEYS
            or any(k.lower().startswith(p) for p in _TRACKING_QUERY_PREFIXES)
        )
    ]
    q.sort()
    return urlunsplit(("https", netloc, path, urlencode(q), ""))


def _idempotency_key(body: s.CreateEstimationIn) -> str | None:
    """A stable per-target key computable BEFORE the URL parse. sreality_id is
    already clean; a URL is canonicalized; a spec is hashed (spec is blocked for
    metered callers, so that arm is defensive only)."""
    if body.sreality_id is not None:
        return f"sid:{int(body.sreality_id)}"
    if body.url:
        return f"url:{_canonical_url(body.url)}"
    if body.spec is not None:
        blob = json.dumps(body.spec.model_dump(mode="json"), sort_keys=True)
        return "spec:" + hashlib.sha256(blob.encode()).hexdigest()[:32]
    return None


def _resolve_entitlement(
    conn: "psycopg.Connection", account_id: str,
) -> tuple[str, bool, int]:
    """(status, estimations_agenda_ok, monthly_agent_quota) for an account.
    Honors an active trial (status='trialing' + unexpired → the plan's trial
    quota). No entitlements row (mig 286 signup makes none) → the default plan,
    status 'active'."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT e.status, (p.agendas->>'estimations') = 'true', "
            "  CASE WHEN e.status = 'trialing' AND (e.current_period_end IS NULL "
            "            OR e.current_period_end > now()) "
            "       THEN coalesce(p.trial_agent_estimations_monthly_quota, 0) "
            "       ELSE coalesce(p.agent_estimations_monthly_quota, 0) END "
            "FROM entitlements e JOIN plans p ON p.key = e.plan "
            "WHERE e.account_id = %s",
            (account_id,),
        )
        row = cur.fetchone()
        if row is not None:
            return str(row[0]), bool(row[1]), int(row[2])
        cur.execute(
            "SELECT (agendas->>'estimations') = 'true', "
            "  coalesce(agent_estimations_monthly_quota, 0) "
            "FROM plans WHERE is_default LIMIT 1",
        )
        d = cur.fetchone()
    if d is None:
        return "active", False, 0
    return "active", bool(d[0]), int(d[1])


def _count_agent_runs_this_month(
    conn: "psycopg.Connection", account_id: str,
) -> int:
    """Non-failed agent runs this calendar month — the budget count. Counting
    pending+running+success (not just success) bounds run CREATION, and a failed
    run frees the slot (matches 'absorb the count on failure')."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM estimation_runs "
            "WHERE account_id = %s AND mode = 'agent' AND status <> 'failed' "
            "  AND created_at >= date_trunc('month', now())",
            (account_id,),
        )
        return int(cur.fetchone()[0])


def _count_inflight_agent_runs(
    conn: "psycopg.Connection", account_id: str,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM estimation_runs "
            "WHERE account_id = %s AND mode = 'agent' "
            "  AND status IN ('pending', 'running')",
            (account_id,),
        )
        return int(cur.fetchone()[0])


def _find_inflight_run(
    conn: "psycopg.Connection", account_id: str, idem_key: str,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM estimation_runs "
            "WHERE account_id = %s AND idempotency_key = %s "
            "  AND status IN ('pending', 'running') "
            "ORDER BY created_at DESC LIMIT 1",
            (account_id, idem_key),
        )
        row = cur.fetchone()
    return _fetch_run(conn, int(row[0])) if row else None


def _concurrency_cap(conn: "psycopg.Connection") -> int:
    try:
        return max(1, int(_load_app_setting(
            conn, CONCURRENCY_CAP_SETTING, CONCURRENCY_CAP_DEFAULT)))
    except (TypeError, ValueError):
        return CONCURRENCY_CAP_DEFAULT


def _prepare_metered_submit(
    conn: "psycopg.Connection",
    claims: dict[str, Any] | None,
    body: s.CreateEstimationIn,
    account_id: str,
) -> _MeterDecision | None:
    """Pre-parse submit gates for a metered (agent, non-admin) submit. Returns
    None for ungated callers; a _MeterDecision (carrying an existing in-flight
    run to short-circuit, or the quota/cap for the atomic INSERT) when cleared;
    raises HTTPException (403 not entitled / raw spec, 429 over quota / too many
    in flight) otherwise. Runs entirely BEFORE the URL parse, so a reject spends
    nothing."""
    if (
        _is_privileged(claims)
        or body.mode != "agent"
        or account_id == SYSTEM_ACCOUNT_ID
        or not _budget_enabled(conn)
    ):
        return None
    if body.spec is not None or body.spec_overrides is not None:
        raise HTTPException(
            status_code=403,
            detail="Custom spec is not permitted for this caller",
        )
    status, estimations_ok, quota = _resolve_entitlement(conn, account_id)
    if status == "canceled" or not estimations_ok:
        raise HTTPException(
            status_code=403, detail="Your plan does not include agent estimations",
        )
    idem_key = _idempotency_key(body)
    if idem_key is None:
        raise HTTPException(status_code=400, detail="A url or sreality_id is required")
    cap = _concurrency_cap(conn)
    existing = _find_inflight_run(conn, account_id, idem_key)
    if existing is not None:
        # Duplicate submit while one is already in flight — return the existing
        # run instead of parsing + charging again (and don't re-enqueue).
        return _MeterDecision(idem_key, quota, cap, short_circuit_run=existing)
    if _count_agent_runs_this_month(conn, account_id) >= quota:
        raise HTTPException(
            status_code=429,
            detail=f"Monthly agent-estimation limit reached ({quota}/mo)",
        )
    if _count_inflight_agent_runs(conn, account_id) >= cap:
        raise HTTPException(
            status_code=429,
            detail="Too many estimations in progress; try again shortly",
        )
    return _MeterDecision(idem_key, quota, cap)


def _record_usage(conn: "psycopg.Connection", run_id: int) -> None:
    """Append a usage_ledger row at a metered agent run's terminal SUCCESS —
    cost = the run's llm_calls sum (mig 355). Best-effort: a ledger hiccup must
    not fail the run (estimation_runs + llm_calls stay the source of truth).
    Reads the run's own account_id/mode so callers needn't thread them."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT account_id, mode FROM estimation_runs WHERE id = %s",
                (run_id,),
            )
            row = cur.fetchone()
        if not row:
            return
        account_id, mode = row
        if mode != "agent" or account_id is None or str(account_id) == SYSTEM_ACCOUNT_ID:
            return
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO usage_ledger "
                "  (account_id, action, cost_usd, estimation_run_id) "
                "SELECT %(aid)s, %(act)s, "
                "  (SELECT sum(cost_usd) FROM llm_calls WHERE estimation_run_id = %(rid)s), "
                "  %(rid)s",
                {"aid": account_id, "act": AGENT_ESTIMATION_ACTION, "rid": run_id},
            )
    except Exception:  # noqa: BLE001 - metering must never fail a completed run
        LOG.exception("usage_ledger write failed for run %s", run_id)


def create_estimation_run(
    conn: "psycopg.Connection",
    sreality_client: "SrealityClient",
    llm_client: "LLMClient",
    body: s.CreateEstimationIn,
    background_tasks: Any | None = None,
    account_id: str | None = None,
    claims: dict[str, Any] | None = None,
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

    `account_id` is the caller's account, resolved from the verified JWT by
    the route handler (Wave 1 W1-1) — hand-threaded because the run persists
    on the service-role connection, which has no JWT/RLS context to read it
    from. Falls back to the platform SYSTEM account for legacy static-token
    callers (today's exact prior behavior — the column no longer relies on
    its own DEFAULT once it's named explicitly in every INSERT).
    """
    account_id = account_id or SYSTEM_ACCOUNT_ID

    # Submit-time gates BEFORE any spend (entitlement + monthly budget +
    # concurrency + idempotency). Ungated for admin/legacy/ClickUp/deterministic;
    # a rejected metered submit raises HTTPException here, before the URL parse.
    meter = _prepare_metered_submit(conn, claims, body, account_id)
    if meter is not None and meter.short_circuit_run is not None:
        return meter.short_circuit_run

    try:
        resolution = _resolve_input(conn, sreality_client, llm_client, body)
    except Exception as exc:
        LOG.warning("URL parse failed: %s", exc)
        return _persist_failed_run(
            conn, body=body, resolution=_resolution_for_parse_failure(body),
            recorder=TraceRecorder(),
            error_msg=f"parse failed: {type(exc).__name__}: {exc}"[:1000],
            extra_warnings=[],
            account_id=account_id,
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
            account_id=account_id,
        )

    purchase, expected_rent, derivation = _derive_yield_inputs(body, resolution)
    body.purchase_price_czk = purchase
    body.expected_monthly_rent_czk = expected_rent
    resolution.yield_input_derivation = derivation

    lane = _job_lane_enabled(conn)
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
                account_id=account_id,
            )
        # Agent runs normally INSERT 'running' so per-turn llm_calls attribute
        # while the loop runs in-process. On the job lane the worker stamps
        # 'running' + claimed_at at claim time, so the row starts 'pending' and
        # the lane's claim (WHERE status='pending') can see it.
        initial_status = "pending" if lane else "running"
    else:
        initial_status = "pending"

    run_id = _insert_run(
        conn,
        account_id=account_id,
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
        subject_attributes=resolution.subject_attributes,
        special_instructions=body.special_instructions,
        contextual_text=body.contextual_text,
        skill_name=skill_obj.name if skill_obj is not None else None,
        skill_version=skill_obj.version if skill_obj is not None else None,
        job_payload=_job_payload(body, resolution) if lane else None,
        idempotency_key=meter.idempotency_key if meter else None,
        gate=meter,
    )

    if run_id is None:
        # The atomic gate rejected the INSERT (a concurrent submit crossed the
        # quota / cap between the pre-parse check and here) or an idempotent
        # duplicate raced in — return the winner if one exists, else 429.
        assert meter is not None
        existing = _find_inflight_run(conn, account_id, meter.idempotency_key)
        if existing is not None:
            return existing
        raise HTTPException(
            status_code=429,
            detail=f"Monthly agent-estimation limit reached ({meter.quota}/mo)",
        )

    if lane:
        # Execution drains off the request process onto the realtime worker's
        # estimation lane (Amendment A10): a 240 s agent run no longer pins a
        # Starlette threadpool token, and a deploy SIGTERM no longer kills it
        # mid-flight with no ledger row. The worker claims this pending row and
        # runs the same heavy path; the client polls GET /estimations/{id}.
        return _fetch_run(conn, run_id) or {}

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
    _record_yield_input_derivation(recorder, resolution)
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
    reference_rent = _reference_rent_for_run(
        conn, recorder, resolution, target, body.estimate_kind,
    )
    summary_text = _summary_line(d, filters.radius_m)
    trace = recorder.to_dict(summary_text)
    merged_warnings = list(resolution.parse_warnings)
    merged_warnings.extend(d.get("warnings") or [])
    _update_run_terminal(
        conn, run_id,
        status="success",
        reference_rent=reference_rent,
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
    renovation_czk: float | None = None,
) -> dict[str, Any] | None:
    """PATCH the operator-tunable yield scenario on an estimation_runs row.

    A body with every number None clears the column back to NULL
    (re-render defaults). Otherwise we store the supplied subset plus
    an `updated_at` stamp so concurrent edits between the SPA and the
    Chrome extension can be reasoned about. `renovation_czk` is a flat
    one-off renovation budget added to the price to form the total
    acquisition cost (the yield denominator).

    Returns the refreshed row, or None when the run id is unknown.
    """
    has_any = any(
        v is not None
        for v in (rent_czk, fond_per_m2_czk, price_czk, renovation_czk)
    )
    if has_any:
        payload: dict[str, Any] = {
            "rent_czk": rent_czk,
            "fond_per_m2_czk": fond_per_m2_czk,
            "price_czk": price_czk,
            "renovation_czk": renovation_czk,
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

    Called from the FastAPI lifespan startup hook to recover rows orphaned by a
    server restart mid-background-task, AND periodically by the realtime worker's
    estimation lane so a run orphaned mid-execution (worker crash) frees its
    concurrency/idempotency slot instead of polling a corpse forever (A10).

    `running` rows are keyed off coalesce(claimed_at, created_at) — the worker
    stamps claimed_at when it starts a run, so a legitimately long agent run is
    timed from when execution BEGAN, not from when it was queued behind a
    backlog. Legacy background-task runs have claimed_at NULL and fall back to
    created_at (identical to the pre-lane behavior). `pending` rows have no claim
    time, so they key off created_at as before.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE estimation_runs "
            "SET status = 'failed', "
            "    error_message = coalesce(error_message, "
            "        'interrupted by server restart') "
            "WHERE status IN ('pending', 'running') "
            "  AND coalesce(claimed_at, created_at) "
            "      < now() - make_interval(mins => %s) "
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
    sreality_ids: list[int] | None = None,
    source_kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Newest-first list, KEYSET-paginated on (created_at, id) DESC.

    `cursor` is the opaque token returned as `next_cursor`; pass it for the
    next page. `offset` remains for any legacy caller (used only when no
    cursor is given). `total` is computed once — on the first page — and is
    null on cursor'd pages (it doesn't change as you scroll).
    """
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
    if sreality_ids:
        # Property-grain fetch: every run on any of the property's child
        # listings (the Listing Detail estimations section).
        where.append("er.input_sreality_id = ANY(%(sreality_ids)s)")
        params["sreality_ids"] = sreality_ids
    if source_kind is not None:
        where.append("er.source_kind = %(source_kind)s")
        params["source_kind"] = source_kind

    filter_sql = "WHERE " + " AND ".join(where) if where else ""

    page_where = list(where)
    if cursor is not None:
        c_ts, c_id = decode_cursor(cursor)
        page_where.append(
            "(er.created_at, er.id) < (%(c_ts)s::timestamptz, %(c_id)s::bigint)"
        )
        params["c_ts"] = c_ts
        params["c_id"] = c_id
    page_where_sql = "WHERE " + " AND ".join(page_where) if page_where else ""

    list_sql = (
        f"SELECT {_LIST_PROJECTION} FROM {_LIST_FROM} {page_where_sql} "
        f"ORDER BY er.created_at DESC, er.id DESC LIMIT %(limit)s OFFSET %(offset)s"
    )
    # Keyset pages ignore offset; only the legacy no-cursor path uses it.
    list_params = {**params, "limit": limit, "offset": 0 if cursor else offset}

    with conn.cursor() as cur:
        cur.execute(list_sql, list_params)
        rows = cur.fetchall()
        total: int | None = None
        # Count on the first page only: any cursor'd page is page 2+ of an
        # infinite scroll, where the cohort total hasn't changed and a fresh
        # count is wasted. The legacy offset path (cursor None) still always
        # returns a total, preserving its contract.
        if cursor is None:
            count_params = {k: params[k] for k in params if k not in ("c_ts", "c_id")}
            cur.execute(
                f"SELECT count(*) FROM estimation_runs er {filter_sql}", count_params
            )
            total_row = cur.fetchone()
            total = int(total_row[0]) if total_row else 0

    id_idx = _LIST_COLUMNS.index("id")
    created_idx = _LIST_COLUMNS.index("created_at")
    next_cursor: str | None = None
    if len(rows) == limit and rows:
        last = rows[-1]
        next_cursor = encode_cursor([last[created_idx].isoformat(), last[id_idx]])

    return {
        "data": [_row_to_dict(_LIST_COLUMNS_OUT, r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "next_cursor": next_cursor,
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


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.replace(",", ".").strip()))
        except ValueError:
            return None
    return None


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

_SUBJECT_ATTR_FIELDS: tuple[str, ...] = (
    "building_type", "condition", "energy_rating",
    "ownership", "furnished",
    "has_balcony", "terrace", "has_lift", "cellar", "garage", "has_parking",
    "disposition", "area_m2", "floor", "locality", "district",
)


def _match_listing_by_url(
    conn: "psycopg.Connection", url: str,
) -> dict[str, Any] | None:
    """Match a pasted URL to an existing scraped `listings` row (any portal we
    already scrape), so a known-portal subject reuses the scraper's parsed
    attributes instead of an LLM parse. Coordinates are required (the
    comparables search is spatial); most-recently-seen row wins. Returns None
    when there's no usable match so the caller falls back to URL parsing.
    """
    canon = source_dispatcher.canonical_url(url)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sreality_id, "
            "ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng, "
            "area_m2, disposition, floor, price_czk, category_type "
            "FROM listings "
            "WHERE geom IS NOT NULL AND source_url IS NOT NULL "
            "AND (source_url = %(url)s OR source_url = %(canon)s "
            "     OR rtrim(source_url, '/') = %(canon)s) "
            "ORDER BY last_seen_at DESC NULLS LAST LIMIT 1",
            {"url": url, "canon": canon},
        )
        row = cur.fetchone()
    if row is None or row[1] is None or row[2] is None:
        return None
    return {
        "sreality_id": int(row[0]),
        "spec": {
            "lat": float(row[1]), "lng": float(row[2]),
            "area_m2": float(row[3]) if row[3] is not None else None,
            "disposition": row[4], "floor": row[5], "exclude_ids": [],
        },
        "price_czk": row[6],
        "category_type": row[7],
    }


def _match_listing_by_id(
    conn: "psycopg.Connection", sreality_id: int,
) -> dict[str, Any] | None:
    """Build a target spec from an already-scraped `listings` row by internal id,
    so a Browse card can estimate a known listing with no URL parse / LLM.
    Mirrors `_match_listing_by_url`; coordinates are required (the comparables
    search is spatial). Returns None when the row is missing or has no geom.

    Subject facts (coords / area / disposition / price / category_type) are
    sourced from the listing's PROPERTY golden record (migration 257) when it
    belongs to an active property, so an estimation launched from ANY portal's
    advert of the same flat resolves the SAME subject — the deterministic
    counterpart of the property-grain MF. `floor` stays per-advert (not a golden
    column). For a singleton property the golden record equals the listing, so
    this is a no-op there. price = the property's canonical current_price_czk
    (the most-recently-seen active ask)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT l.sreality_id, "
            "ST_Y(COALESCE(p.geom, l.geom)::geometry) AS lat, "
            "ST_X(COALESCE(p.geom, l.geom)::geometry) AS lng, "
            "COALESCE(p.area_m2, l.area_m2) AS area_m2, "
            "COALESCE(p.disposition, l.disposition) AS disposition, "
            "l.floor, "
            "COALESCE(p.current_price_czk, l.price_czk) AS price_czk, "
            "COALESCE(p.category_type, l.category_type) AS category_type "
            "FROM listings l "
            "LEFT JOIN properties p ON p.id = l.property_id AND p.status = 'active' "
            "WHERE l.sreality_id = %(id)s LIMIT 1",
            {"id": int(sreality_id)},
        )
        row = cur.fetchone()
    if row is None or row[1] is None or row[2] is None:
        return None
    return {
        "sreality_id": int(row[0]),
        "spec": {
            "lat": float(row[1]), "lng": float(row[2]),
            "area_m2": float(row[3]) if row[3] is not None else None,
            "disposition": row[4], "floor": row[5], "exclude_ids": [],
        },
        "price_czk": row[6],
        "category_type": row[7],
    }


def latest_rent_estimations_by_listing(
    conn: "psycopg.Connection", sreality_ids: list[int],
) -> dict[int, dict[str, Any]]:
    """Latest RENT estimation_runs row per listing id (any status), for the
    Browse cards' on-card estimate chip. Returns {sreality_id: {...}}; ids with
    no rent run are absent."""
    if not sreality_ids:
        return {}
    ids = [int(x) for x in sreality_ids]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (input_sreality_id) "
            "input_sreality_id, id, status, estimate_kind, "
            "gross_yield_pct, estimated_monthly_rent_czk, created_at "
            "FROM estimation_runs "
            "WHERE input_sreality_id = ANY(%(ids)s) AND estimate_kind = 'rent' "
            "ORDER BY input_sreality_id, created_at DESC",
            {"ids": ids},
        )
        rows = cur.fetchall()
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        out[int(r[0])] = {
            "sreality_id": int(r[0]),
            "run_id": int(r[1]),
            "status": r[2],
            "estimate_kind": r[3],
            "gross_yield_pct": float(r[4]) if r[4] is not None else None,
            "estimated_monthly_rent_czk": int(r[5]) if r[5] is not None else None,
            "created_at": r[6].isoformat() if r[6] is not None else None,
        }
    return out


def _subject_attributes_from_result(
    result: "source_dispatcher.ParseResult",
) -> dict[str, Any] | None:
    """Typed subject attributes (mirroring listings_public field names) for a
    parsed subject with no resolved listings row — lets the UI render it like a
    listing. Pulls from the parse result's wide_spec / full_extraction. None
    when nothing typed was extracted."""
    listing = _listing_from_result(result)
    spec = result.spec or {}
    fx = result.full_extraction or {}

    def from_extraction(name: str) -> Any:
        env = fx.get(name)
        return env["value"] if isinstance(env, dict) and "value" in env else None

    attrs: dict[str, Any] = {}
    for f in _SUBJECT_ATTR_FIELDS:
        if f in ("disposition", "area_m2", "floor"):
            attrs[f] = spec.get(f)
        elif f in listing and listing.get(f) is not None:
            attrs[f] = listing.get(f)
        else:
            attrs[f] = from_extraction(f)
    if not any(v is not None for v in attrs.values()):
        return None
    return attrs


def _resolve_input(
    conn: "psycopg.Connection",
    sreality_client: "SrealityClient",
    llm_client: "LLMClient",
    body: s.CreateEstimationIn,
) -> _Resolution:
    """Build a _Resolution from the request body.

    URL path: if the link is from a portal we already scrape and the listing is
    already in our DB, reuse that scraped row (deterministic, no LLM). Otherwise
    dispatch through scraper.source_dispatcher (sreality → deterministic flow;
    any other domain → LLM-driven per-source parser), capturing the parsed
    attributes so the subject can still render like a listing. Spec path: pass
    through with all parse-* fields None. sreality_id path: build the target
    from the scraped `listings` row by id (no parse, no LLM).
    """
    if body.sreality_id is not None:
        matched = _match_listing_by_id(conn, body.sreality_id)
        if matched is None:
            raise ValueError(
                f"listing {body.sreality_id} not found or missing coordinates"
            )
        spec = dict(matched["spec"])
        if body.spec_overrides:
            spec = {**spec, **body.spec_overrides}
        return _Resolution(
            input_url=None,
            input_sreality_id=matched["sreality_id"],
            target_spec=spec,
            source_kind=None,
            parse_confidence=None,
            parse_confidence_per_field=None,
            source_html=None,
            parse_warnings=[],
            subject_listing_price_czk=_coerce_int(matched.get("price_czk")),
            subject_listing_category_type=matched.get("category_type"),
            subject_attributes=None,
        )
    if body.url is not None:
        # Non-sreality portals don't resolve to a listings row through the
        # dispatcher, but we scrape them — so try the already-scraped row first.
        # sreality keeps its deterministic re-fetch branch in the dispatcher.
        if source_dispatcher.classify_url(body.url) != "sreality":
            matched = _match_listing_by_url(conn, body.url)
            if matched is not None:
                spec = dict(matched["spec"])
                if body.spec_overrides:
                    spec = {**spec, **body.spec_overrides}
                return _Resolution(
                    input_url=body.url,
                    input_sreality_id=matched["sreality_id"],
                    target_spec=spec,
                    source_kind=None,
                    parse_confidence=None,
                    parse_confidence_per_field=None,
                    source_html=None,
                    parse_warnings=[],
                    subject_listing_price_czk=_coerce_int(matched.get("price_czk")),
                    subject_listing_category_type=matched.get("category_type"),
                    subject_attributes=None,
                )

        result = source_dispatcher.parse_listing_url(
            body.url,
            sreality_client=sreality_client,
            llm_client=llm_client,
            conn=conn,
        )
        spec = dict(result.spec)
        if body.spec_overrides:
            spec = {**spec, **body.spec_overrides}
        subject_listing = _listing_from_result(result)
        # When the parse resolved to a real listings row (sreality), the UI reads
        # listings_public; otherwise carry the parsed attributes for the UI.
        subject_attributes = (
            None
            if result.sreality_id is not None
            else _subject_attributes_from_result(result)
        )
        return _Resolution(
            input_url=body.url,
            input_sreality_id=result.sreality_id,
            target_spec=spec,
            source_kind=result.source_kind,
            parse_confidence=result.parse_confidence,
            parse_confidence_per_field=result.parse_confidence_per_field,
            source_html=result.source_html,
            parse_warnings=list(result.warnings),
            subject_listing_price_czk=_coerce_int(subject_listing.get("price_czk")),
            subject_listing_category_type=subject_listing.get("category_type"),
            subject_attributes=subject_attributes,
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


def _derive_yield_inputs(
    body: s.CreateEstimationIn, resolution: _Resolution,
) -> tuple[int | None, int | None, dict[str, Any] | None]:
    """Fill in yield-formula inputs from the subject listing when the
    operator didn't supply them.

    Rent estimate + sale listing → use listing.price_czk as purchase price.
    Sale estimate + rental listing → use listing.price_czk as expected rent.
    Operator-supplied body values always win.

    Returns (purchase_price_czk, expected_monthly_rent_czk, derivation_log).
    derivation_log is None when nothing was derived; otherwise a dict
    describing the source for the trace step.
    """
    purchase = body.purchase_price_czk
    expected_rent = body.expected_monthly_rent_czk
    listing_price = resolution.subject_listing_price_czk
    listing_kind = resolution.subject_listing_category_type
    if listing_price is None or listing_price <= 0:
        return purchase, expected_rent, None
    if body.estimate_kind == "rent" and purchase is None and listing_kind == "prodej":
        return listing_price, expected_rent, {
            "field": "purchase_price_czk",
            "value": listing_price,
            "source": "subject_listing.price_czk",
            "subject_category_type": listing_kind,
        }
    if body.estimate_kind == "sale" and expected_rent is None and listing_kind == "pronajem":
        return purchase, listing_price, {
            "field": "expected_monthly_rent_czk",
            "value": listing_price,
            "source": "subject_listing.price_czk",
            "subject_category_type": listing_kind,
        }
    return purchase, expected_rent, None


def _record_yield_input_derivation(
    recorder: TraceRecorder, resolution: _Resolution,
) -> None:
    derivation = resolution.yield_input_derivation
    if derivation is None:
        return
    with recorder.computation("derive yield inputs from subject listing") as step:
        step.set_summary(dict(derivation))


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
    account_id: str | None = None,
) -> dict[str, Any]:
    trace = recorder.to_dict(f"failed: {error_msg.split(':', 1)[0]}")
    merged = list(resolution.parse_warnings) + list(extra_warnings or [])
    run_id = _insert_run(
        conn,
        account_id=account_id or SYSTEM_ACCOUNT_ID,
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


_STANDARD_MATERIALS = frozenset({"panel", "cihla"})


def _load_subject_amenities(
    conn: "psycopg.Connection", sreality_id: int,
) -> dict[str, Any] | None:
    """Amenity flags + condition/building_type for the reference-rent calc,
    sourced from the listing's PROPERTY golden record (migration 257) when it
    belongs to an active property — so the run's MF reference rent uses the same
    OR-unioned amenities as the property-grain MF (a portal that under-parsed an
    amenity no longer under-states the estimate). COALESCE keeps the listing's
    own value for a singleton / pre-attach row (where they are equal)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(p.has_balcony, l.has_balcony), "
            "COALESCE(p.terrace, l.terrace), COALESCE(p.furnished, l.furnished), "
            "COALESCE(p.garage, l.garage), COALESCE(p.has_lift, l.has_lift), "
            "COALESCE(p.building_type, l.building_type), "
            "COALESCE(p.condition, l.condition) "
            "FROM listings l "
            "LEFT JOIN properties p ON p.id = l.property_id AND p.status = 'active' "
            "WHERE l.sreality_id = %s",
            (sreality_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    keys = ("has_balcony", "terrace", "furnished", "garage", "has_lift",
            "building_type", "condition")
    return dict(zip(keys, row))


def _subject_amenities(
    conn: "psycopg.Connection", resolution: _Resolution,
) -> tuple[dict[str, bool], bool]:
    """Amenity flags + new-build signal for the reference-rent calc.

    sreality runs read the authoritative `listings` columns; URL / spec
    runs fall back to whatever the parsed spec carries (best-effort — the
    base reference rent still computes from lat/lng/area/disposition).
    """
    src: dict[str, Any] | None = None
    if resolution.input_sreality_id is not None:
        src = _load_subject_amenities(conn, resolution.input_sreality_id)
    if src is None:
        src = resolution.target_spec or {}
    is_novostavba = src.get("condition") == "novostavba"
    building_type = src.get("building_type")
    amenities = {
        "balcony": bool(src.get("has_balcony")),
        "terrace": bool(src.get("terrace")),
        "furnished": src.get("furnished") == "ano",
        "garage": bool(src.get("garage")),
        "elevator": bool(src.get("has_lift")),
        "other_material": bool(
            is_novostavba
            and building_type
            and building_type not in _STANDARD_MATERIALS
        ),
    }
    return amenities, is_novostavba


def _reference_rent_for_run(
    conn: "psycopg.Connection",
    recorder: TraceRecorder,
    resolution: _Resolution,
    target: TargetSpec,
    estimate_kind: str,
) -> dict[str, Any] | None:
    """MF Cenová mapa secondary reference, emitted as a trace step.

    Rent estimates only; best-effort (compute_reference_rent swallows
    its own errors and returns None on any miss).
    """
    if estimate_kind != "rent":
        return None
    with recorder.computation("reference rent (Cenová mapa MF)") as step:
        try:
            amenities, is_novostavba = _subject_amenities(conn, resolution)
            ref = compute_reference_rent(
                conn,
                lat=target.lat, lng=target.lng, area_m2=target.area_m2,
                disposition=target.disposition,
                amenities=amenities, is_novostavba=is_novostavba,
            )
        except Exception:  # noqa: BLE001 - secondary reference never fails a run
            ref = None
        if ref is None:
            step.set_summary({"matched": False})
        else:
            step.set_summary({
                "matched": True,
                "territory": ref["territory"]["name"],
                "vk": ref["vk"],
                "is_novostavba": ref["is_novostavba"],
                "base_per_m2": ref["base_per_m2"],
                "total_per_m2": ref["total_per_m2"],
                "monthly_rent_czk": ref["monthly_rent_czk"],
            })
    return ref


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
        lifecycle=(
            body.lifecycle if body.lifecycle is not None else defaults.lifecycle
        ),
        floor_band=body.floor_band,
        portals=body.portals,
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
        building_condition_level_max=body.building_condition_level_max,
        apartment_condition_level_min=body.apartment_condition_level_min,
        apartment_condition_level_max=body.apartment_condition_level_max,
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
    _record_yield_input_derivation(recorder, resolution)

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
    reference_rent = (
        _reference_rent_for_run(
            conn, recorder, resolution, target, body.estimate_kind,
        )
        if status == "success"
        else None
    )
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
        comparables_excluded=d.get("comparables_excluded") or None,
        trace=trace,
        warnings=merged_warnings or None,
        error_message=err,
        reference_rent=reference_rent,
    )
    flush_trace_payloads(conn, run_id, recorder)
    if status == "success":
        # Meter the successful agent run (no-op for admin/SYSTEM/deterministic).
        # A failed run consumes no quota and writes no ledger row.
        _record_usage(conn, run_id)


def _update_run_terminal(
    conn: "psycopg.Connection",
    run_id: int,
    **fields: Any,
) -> None:
    """Parameterised UPDATE that writes only the supplied columns."""
    for k in (
        "comparables_used", "comparables_excluded",
        "trace", "warnings", "reference_rent",
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


def _insert_run(
    conn: "psycopg.Connection",
    *,
    gate: "_MeterDecision | None" = None,
    **fields: Any,
) -> int | None:
    """INSERT an estimation_runs row, returning its id.

    With `gate` (a metered submit), the INSERT is the ATOMIC enforcement point
    (A9): an `INSERT ... SELECT WHERE (monthly non-failed count) < quota AND
    (in-flight count) < cap ON CONFLICT (account_id, idempotency_key) DO NOTHING`
    — budget + concurrency + idempotency in one write, with zero check-then-act
    race. No row back (returns None) means the gate rejected it or an idempotent
    duplicate raced in; the caller resolves the winner or 429s. Without `gate`
    (admin/internal/deterministic) it's the plain VALUES insert, which must
    always return an id (raises otherwise, unchanged)."""
    for col in _INSERT_COLUMNS:
        fields.setdefault(col, None)
    for k in (
        "input_spec", "comparables_used", "comparables_excluded",
        "trace", "warnings",
        "parse_confidence_per_field", "subject_attributes",
        "scenario", "reference_rent",
    ):
        if fields.get(k) is not None:
            fields[k] = Jsonb(fields[k])
    cols = list(_INSERT_COLUMNS)
    values = [f"%({c})s" for c in cols]
    # Dual-write (migration 324): stamp the surrogate listings.id alongside the
    # legacy smart key, resolved inline so no extra round-trip is needed.
    _sid_at = cols.index("input_sreality_id")
    cols.insert(_sid_at + 1, "input_listing_id")
    values.insert(
        _sid_at + 1,
        "(SELECT id FROM listings WHERE sreality_id = %(input_sreality_id)s)",
    )
    # Optional worker-lane execution snapshot (migration 349). Kept out of
    # _RUN_COLUMNS so it never rides the API read surface; only ever present when
    # the job lane is enabled, and cleared to NULL by the lane at terminal.
    if fields.get("job_payload") is not None:
        fields["job_payload"] = Jsonb(fields["job_payload"])
        cols.append("job_payload")
        values.append("%(job_payload)s")
    # Idempotency / single-in-flight key for metered submits (migration 355),
    # likewise out of _RUN_COLUMNS. NULL for ungated callers.
    if fields.get("idempotency_key") is not None:
        cols.append("idempotency_key")
        values.append("%(idempotency_key)s")
    cols_sql = ", ".join(cols)
    placeholders = ", ".join(values)
    if gate is None:
        sql = (
            f"INSERT INTO estimation_runs ({cols_sql}) "
            f"VALUES ({placeholders}) RETURNING id"
        )
    else:
        fields["_g_quota"] = gate.quota
        fields["_g_cap"] = gate.concurrency_cap
        sql = (
            f"INSERT INTO estimation_runs ({cols_sql}) "
            f"SELECT {placeholders} "
            "WHERE (SELECT count(*) FROM estimation_runs "
            "         WHERE account_id = %(account_id)s AND mode = 'agent' "
            "           AND status <> 'failed' "
            "           AND created_at >= date_trunc('month', now())) < %(_g_quota)s "
            "  AND (SELECT count(*) FROM estimation_runs "
            "         WHERE account_id = %(account_id)s AND mode = 'agent' "
            "           AND status IN ('pending', 'running')) < %(_g_cap)s "
            "ON CONFLICT (account_id, idempotency_key) "
            "  WHERE status IN ('pending', 'running') AND idempotency_key IS NOT NULL "
            "DO NOTHING "
            "RETURNING id"
        )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql, fields)
        row = cur.fetchone()
        if row is None:
            if gate is not None:
                return None
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
