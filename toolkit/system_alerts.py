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
    conn: Any, check_key: str, message: str, *, day: str | None = None,
) -> bool:
    """Insert a system_health notification_dispatches row; return whether one was inserted.

    Idempotent per (check_key, day): `dedupe_key = sys:{check_key}:{day}` with the day
    defaulting to today (UTC), so a check that stays red re-alarms at most once a day.
    ON CONFLICT (dedupe_key) DO NOTHING makes a repeat call a no-op (returns False).
    """
    dedupe_key = f"sys:{check_key}:{day or _today_utc()}"
    channels = _system_health_channels(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO notification_dispatches "
            "  (source_kind, change_kind, channel, status, message, dedupe_key, target_channels) "
            "VALUES ('system_health', 'system_alert', 'in_app', 'sent', %s, %s, %s::text[]) "
            "ON CONFLICT (dedupe_key) DO NOTHING",
            (message, dedupe_key, channels),
        )
        return (cur.rowcount or 0) > 0
