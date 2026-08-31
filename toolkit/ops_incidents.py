"""ops_incidents — one open row per failure REASON, exiting through the one bell.

W3.2/W3.3 of the reliability program (`docs/design/reliability-program.md`, migration
462). Two producers feed it — `scraper.portal_runner`'s crash path (in-process, at
t+0, with the exception object in hand) and `scripts/record_workflow_failures.py`'s
log-tail backstop (the majority of the corpus: CI, backfills, jobs, LLM lanes, and
every red with no Python traceback). Both call `record_failure_signature`.

Deliberately dependency-free (stdlib + a caller-passed psycopg connection), like
`toolkit.system_alerts` next to it: the Actions poller installs base deps only, so a
single `api/` import here would silently break a lane that has no test.

**It emits no alert of its own.** Crossing the onset threshold writes exactly one
`system_health` `notification_dispatches` row through `toolkit.system_alerts`, and the
shipped outbox delivers it. The reviewed proposal's `ops_incident_alerts` +
`ops_alert_email` + `toolkit/ops_alerts.py` was rejected on purpose: a second bell
namespace is the shape the WS4 rebuild removed.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from typing import Any

from toolkit.system_alerts import emit_system_alert

LOG = logging.getLogger(__name__)

# Code fallbacks for the operator-editable scalars migration 462 seeds into
# app_settings.pipeline_check_thresholds. See that file for how each was measured.
DEFAULT_MIN_FAILURES = 2
DEFAULT_MAX_AGE_HOURS = 168
DEFAULT_LOG_EXCERPT_BYTES = 4000

# The second onset arm. A fleet fan-out should not wait on a timer it already has
# breadth for: on 2026-08-26 the signature reached its 2nd workflow 8 minutes after
# onset, while a same-workflow 2nd failure needs a whole cadence (up to 164 min under
# the Actions throttle).
_BREADTH_ARM_WORKFLOWS = 2

_UPSERT_SQL = """
INSERT INTO ops_incidents
    (signature, workflow_paths, origins, sample_run_url, sample_excerpt)
VALUES (%(signature)s, %(paths)s::text[], %(origins)s::text[], %(url)s, %(excerpt)s)
ON CONFLICT (signature) WHERE resolved_at IS NULL DO UPDATE SET
    failure_count = ops_incidents.failure_count + 1,
    last_seen_at  = now(),
    -- Append-if-new rather than a dedup subquery: every call contributes at most one
    -- path, so `= any(...)` is the whole dedup, and ON CONFLICT DO UPDATE stays free of
    -- sub-selects. An empty excluded array yields NULL = any(...) -> NULL -> the ELSE
    -- branch, which concatenates nothing.
    workflow_paths = CASE
        WHEN excluded.workflow_paths[1] = any(ops_incidents.workflow_paths)
        THEN ops_incidents.workflow_paths
        ELSE ops_incidents.workflow_paths || excluded.workflow_paths END,
    origins = CASE
        WHEN excluded.origins[1] = any(ops_incidents.origins)
        THEN ops_incidents.origins
        ELSE ops_incidents.origins || excluded.origins END,
    sample_run_url = coalesce(ops_incidents.sample_run_url, excluded.sample_run_url),
    sample_excerpt = coalesce(ops_incidents.sample_excerpt, excluded.sample_excerpt)
RETURNING id, failure_count, first_seen_at, workflow_paths, sample_run_url,
          sample_excerpt, alerted_at
"""

_RESOLVE_ON_SUCCESS_SQL = """
UPDATE ops_incidents i
   SET resolved_at = now(), resolve_reason = 'success'
 WHERE i.resolved_at IS NULL
   AND coalesce(array_length(i.workflow_paths, 1), 0) > 0
   AND NOT EXISTS (
       SELECT 1
         FROM unnest(i.workflow_paths) AS p
         LEFT JOIN workflow_run_health h ON h.workflow_path = p
        WHERE h.last_success_at IS NULL OR h.last_success_at <= i.last_seen_at)
RETURNING id, signature, alerted_at
"""

_RESOLVE_MAX_AGE_SQL = """
UPDATE ops_incidents
   SET resolved_at = now(), resolve_reason = 'max_age'
 WHERE resolved_at IS NULL
   AND last_seen_at < now() - make_interval(hours => %(hours)s::int)
RETURNING id, signature, alerted_at
"""


def actions_context() -> tuple[str | None, str | None]:
    """`(workflow_path, run_url)` from the Actions env, or `(None, None)` off-CI.

    The in-process producer must stamp the SAME `workflow_path` shape the poller
    writes (`.github/workflows/x.yml`), or the success-based resolver's join against
    `workflow_run_health` finds nothing and the incident can only ever age out. The
    always-on Railway worker legitimately has neither — that is what `origins` is for.
    """
    ref = os.environ.get("GITHUB_WORKFLOW_REF") or ""
    path: str | None = None
    if ".github/workflows/" in ref:
        tail = ref.split("/", 2)[2] if ref.count("/") >= 2 else ref
        path = tail.split("@", 1)[0] or None
    server = os.environ.get("GITHUB_SERVER_URL") or "https://github.com"
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    url = f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else None
    return path, url


def _thresholds(conn: Any) -> dict[str, float]:
    """The operator-editable scalars, with the code defaults as fallbacks."""
    raw: Any = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM app_settings WHERE key = 'pipeline_check_thresholds'"
            )
            row = cur.fetchone()
        raw = row[0] if row else None
    except Exception as exc:  # noqa: BLE001 — a settings read must never break a producer
        LOG.warning("ops_incidents: threshold read failed (%s); using code defaults", exc)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = None
    out = {
        "ops_incident_min_failures": float(DEFAULT_MIN_FAILURES),
        "ops_incident_max_age_hours": float(DEFAULT_MAX_AGE_HOURS),
        "ops_incident_log_excerpt_bytes": float(DEFAULT_LOG_EXCERPT_BYTES),
    }
    if isinstance(raw, dict):
        for k in list(out):
            v = raw.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = float(v)
    return out


def excerpt_byte_budget(conn: Any) -> int:
    return int(_thresholds(conn)["ops_incident_log_excerpt_bytes"])


def record_failure(
    conn: Any,
    signature: str,
    *,
    workflow_path: str | None = None,
    origin: str | None = None,
    sample_run_url: str | None = None,
    sample_excerpt: str | None = None,
) -> dict[str, Any]:
    """Open or bump the OPEN incident for `signature`; return its state.

    The upsert targets the partial unique index (`WHERE resolved_at IS NULL`), so a
    previously resolved incident never blocks a fresh one — a recurrence is a new
    incident, not a reopened row."""
    params = {
        "signature": signature,
        "paths": [workflow_path] if workflow_path else [],
        "origins": [origin] if origin else [],
        "url": sample_run_url,
        "excerpt": sample_excerpt,
    }
    with conn.cursor() as cur:
        cur.execute(_UPSERT_SQL, params)
        row = cur.fetchone()
    if not row:
        return {}
    inc_id, count, first_seen, paths, url, excerpt, alerted_at = row
    return {
        "id": inc_id,
        "signature": signature,
        "failure_count": int(count),
        "first_seen_at": first_seen,
        "workflow_paths": list(paths or []),
        "sample_run_url": url,
        "sample_excerpt": excerpt,
        "alerted_at": alerted_at,
        "opened": int(count) == 1,
    }


def _format_alert(incident: dict[str, Any]) -> str:
    paths = incident.get("workflow_paths") or []
    first = incident.get("first_seen_at")
    since = first.strftime("%Y-%m-%d %H:%M UTC") if isinstance(first, _dt.datetime) else "?"
    lines = [
        f"Ops incident #{incident['id']}: {incident['signature']}",
        f"{incident['failure_count']} failures since {since}"
        + (f" across {len(paths)} workflow(s): " + ", ".join(paths) if paths else ""),
    ]
    if incident.get("sample_run_url"):
        lines.append(f"Sample run: {incident['sample_run_url']}")
    if incident.get("sample_excerpt"):
        lines.append("---")
        lines.append(str(incident["sample_excerpt"]))
    return "\n".join(lines)


def maybe_alert(conn: Any, incident: dict[str, Any], *, min_failures: int | None = None) -> bool:
    """Write the ONE onset dispatch, if this incident has earned it and has none yet.

    Two arms, whichever trips first: `failure_count >= min_failures`, or the signature
    already spans `_BREADTH_ARM_WORKFLOWS` distinct workflows."""
    if not incident or incident.get("alerted_at") is not None:
        return False
    threshold = (
        min_failures
        if min_failures is not None
        else int(_thresholds(conn)["ops_incident_min_failures"])
    )
    breadth = len(incident.get("workflow_paths") or [])
    if incident["failure_count"] < threshold and breadth < _BREADTH_ARM_WORKFLOWS:
        return False
    # Claim the alert BEFORE emitting: two producers can observe the same incident in
    # the same second, and the dispatch's dedupe_key alone would let the second one
    # believe it alerted. rowcount=0 means somebody else already owns this onset.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ops_incidents SET alerted_at = now(), alert_count = alert_count + 1 "
            "WHERE id = %s AND alerted_at IS NULL",
            (incident["id"],),
        )
        if (cur.rowcount or 0) == 0:
            return False
    emit_system_alert(
        conn,
        "ops_incident",
        _format_alert(incident),
        dedupe_key=f"sys:ops_incident:{incident['id']}:onset",
    )
    return True


def record_failure_signature(
    conn: Any,
    signature: str,
    *,
    workflow_path: str | None = None,
    origin: str | None = None,
    sample_run_url: str | None = None,
    sample_excerpt: str | None = None,
) -> dict[str, Any]:
    """The one entry point both producers call: upsert, then alert if earned."""
    incident = record_failure(
        conn, signature,
        workflow_path=workflow_path, origin=origin,
        sample_run_url=sample_run_url, sample_excerpt=sample_excerpt,
    )
    if incident:
        incident["alerted"] = maybe_alert(conn, incident)
    return incident


def open_signature_by_workflow(conn: Any, *, within_minutes: int = 60) -> dict[str, str]:
    """`{workflow_path: signature}` for OPEN incidents touched recently.

    The poller's API-budget saver: if the in-process producer already recorded why a
    workflow is failing, downloading its job log to re-derive the same signature buys
    nothing."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT signature, workflow_paths FROM ops_incidents "
            " WHERE resolved_at IS NULL "
            "   AND last_seen_at > now() - make_interval(mins => %s::int) "
            " ORDER BY last_seen_at DESC",
            (int(within_minutes),),
        )
        rows = cur.fetchall()
    out: dict[str, str] = {}
    for signature, paths in rows:
        for p in paths or []:
            out.setdefault(str(p), str(signature))
    return out


def _emit_recovery(conn: Any, rows: list[tuple[Any, ...]], reason: str) -> int:
    """Close the loop only for incidents that actually rang. An incident that never
    alerted resolving silently is correct; one that did must say so, or the operator
    is left holding a red they cannot tell is over."""
    n = 0
    for inc_id, signature, alerted_at in rows:
        if alerted_at is None:
            continue
        if emit_system_alert(
            conn, "ops_incident",
            f"Resolved (#{inc_id}, {reason}): {signature}",
            dedupe_key=f"sys:ops_incident:{inc_id}:resolved",
        ):
            n += 1
    return n


def auto_resolve(conn: Any, *, max_age_hours: int | None = None) -> dict[str, int]:
    """Close what the world has answered for. Two paths, both mandatory.

    `success` is the real signal — every member workflow posted a
    `workflow_run_health.last_success_at` newer than the incident's `last_seen_at`.
    `max_age` is the backstop for the incidents that path can never reach: a workflow
    that is retired, disabled, unscheduled or renamed (a rename forks `workflow_path`)
    never posts a success, and an incident from the always-on worker has no member
    workflow at all."""
    hours = (
        max_age_hours
        if max_age_hours is not None
        else int(_thresholds(conn)["ops_incident_max_age_hours"])
    )
    with conn.cursor() as cur:
        cur.execute(_RESOLVE_ON_SUCCESS_SQL)
        by_success = cur.fetchall()
        cur.execute(_RESOLVE_MAX_AGE_SQL, {"hours": int(hours)})
        by_age = cur.fetchall()
    notified = _emit_recovery(conn, list(by_success), "member workflows recovered")
    notified += _emit_recovery(conn, list(by_age), f"no activity for {int(hours)}h")
    return {
        "resolved_success": len(by_success),
        "resolved_max_age": len(by_age),
        "recovery_alerts": notified,
    }


def resolve_incident(conn: Any, incident_id: int, note: str = "") -> bool:
    """The operator's manual close. There is no admin route for this yet: the Health
    page renders `workflow_failures`, not incidents, and inventing a surface for a
    table whose first week of data nobody has seen would be guessing."""
    reason = f"manual: {note}".strip().rstrip(":") if note else "manual"
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ops_incidents SET resolved_at = now(), resolve_reason = %s "
            " WHERE id = %s AND resolved_at IS NULL",
            (reason, int(incident_id)),
        )
        return (cur.rowcount or 0) > 0
