"""In-app system-health alerts through the unified notification feed.

A `system_health` producer for `notification_dispatches` (migration 274): a red
pipeline-verification check — or a stalled LLM pipeline — inserts one append-only
dispatch row so the SPA nav bell badge lights up, reusing the whole existing
Notifications surface instead of a parallel alerting path.

Dependency-free by design (stdlib + a caller-passed psycopg connection) so both the
FastAPI service and the standalone verification script can call it.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any


def _today_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def _system_health_channels(conn: Any) -> list[str]:
    """Operator-chosen external channels for system alerts (default [] = in-app only)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM app_settings WHERE key = 'system_health_channels'"
        )
        row = cur.fetchone()
    raw = row[0] if row and row[0] is not None else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, str) and c]


def emit_system_alert(
    conn: Any, check_key: str, message: str, *,
    day: str | None = None, dedupe_key: str | None = None,
) -> bool:
    """Insert a system_health notification_dispatches row; return whether one was inserted.

    Idempotent via `dedupe_key` + `ON CONFLICT (dedupe_key) DO NOTHING` (a repeat is a
    no-op returning False). When `dedupe_key` is not given it falls back to the legacy
    per-UTC-day key `sys:{check_key}:{day or today}` (at most one alert/day). The
    transition emitter passes an explicit edge-anchored key instead — see
    `emit_transition_alerts`.
    """
    key = dedupe_key or f"sys:{check_key}:{day or _today_utc()}"
    channels = _system_health_channels(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO notification_dispatches "
            "  (source_kind, change_kind, channel, status, message, dedupe_key, target_channels) "
            "VALUES ('system_health', 'system_alert', 'in_app', 'sent', %s, %s, %s::text[]) "
            "ON CONFLICT (dedupe_key) DO NOTHING",
            (message, key, channels),
        )
        return (cur.rowcount or 0) > 0


def _iso(run_at: _dt.datetime) -> str:
    """Stable second-resolution UTC stamp for edge-anchored dedupe keys."""
    return run_at.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def latest_statuses(conn: Any) -> dict[str, str]:
    """The most recently STORED status per check_key (call BEFORE writing this run's
    rows, so it reflects the previous run — the baseline transitions compare against)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (check_key) check_key, status "
            "FROM pipeline_check_results ORDER BY check_key, run_at DESC"
        )
        return {str(k): str(s) for (k, s) in cur.fetchall()}


def emit_transition_alerts(
    conn: Any,
    results: list[dict[str, Any]],
    prev_statuses: dict[str, str],
    run_at: _dt.datetime,
) -> dict[str, int]:
    """Ring the bell on STATE TRANSITIONS, not on every red run.

    Per check, comparing the previous stored status (`prev_statuses`) to this run's:
      * onset  (… → fail):   one alert, keyed `sys:{k}:onset:{run_at}` — fires exactly
                             once per incident, because every later run sees prev=fail and
                             takes the silent ongoing branch (no streak tracking needed).
      * recovery (fail → …): one "recovered" alert, keyed `sys:{k}:recovery:{run_at}`, so
                             the feed auto-resolves instead of leaving a stale red row.
      * ongoing (fail → fail): silent — a persistent failure stays visible on /health and,
                             for the LLM liveness check, in the hourly exit-1 email.
    ok↔warn transitions never ring (warn is dashboard-only). Idempotent across cadences: a
    6h full run and an hourly subset run share the same edge keys via ON CONFLICT, so the
    incident is alerted once regardless of which cadence first observes each edge.
    """
    counts = {"onset": 0, "recovery": 0}
    stamp = _iso(run_at)
    for r in results:
        k = r["check_key"]
        curr = r["status"]
        prev = prev_statuses.get(k)
        if curr == "fail" and prev != "fail":
            msg = r.get("message") or f"Pipeline check '{k}' failed."
            if emit_system_alert(conn, k, msg, dedupe_key=f"sys:{k}:onset:{stamp}"):
                counts["onset"] += 1
        elif curr != "fail" and prev == "fail":
            msg = f"✓ Recovered: '{k}' is healthy again (now {curr})."
            if emit_system_alert(conn, k, msg, dedupe_key=f"sys:{k}:recovery:{stamp}"):
                counts["recovery"] += 1
    return counts
