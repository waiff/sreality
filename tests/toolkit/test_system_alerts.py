"""Hermetic tests for toolkit.system_alerts — no DB, a scripted fake connection."""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from toolkit.system_alerts import (
    emit_system_alert,
    emit_transition_alerts,
    latest_statuses,
)


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((sql, params))
        if "app_settings" in sql:
            self._conn._fetch = (
                None if self._conn.channels_value is _MISSING
                else (self._conn.channels_value,)
            )
        elif "pipeline_check_results" in sql:
            self._conn._fetchall = self._conn.latest_rows
        elif "INSERT INTO notification_dispatches" in sql:
            self._conn.insert_sql = sql
            self._conn.insert_params = params
            self._conn.inserts.append(params)
            self.rowcount = self._conn.insert_rowcount
            return
        self.rowcount = 0

    def fetchone(self) -> Any:
        return self._conn._fetch

    def fetchall(self) -> Any:
        return self._conn._fetchall

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


_MISSING = object()


class _FakeConn:
    def __init__(
        self, *, channels_value: Any = _MISSING, insert_rowcount: int = 1,
        latest_rows: list[tuple[str, str]] | None = None,
    ) -> None:
        self.channels_value = channels_value
        self.insert_rowcount = insert_rowcount
        self.executed: list[tuple[str, Any]] = []
        self.insert_sql: str | None = None
        self.insert_params: Any = None
        self.inserts: list[Any] = []
        self.latest_rows = latest_rows or []
        self._fetch: Any = None
        self._fetchall: Any = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


_RUN_AT = _dt.datetime(2026, 7, 9, 15, 37, 0, tzinfo=_dt.timezone.utc)


def _dedupe_keys(conn: _FakeConn) -> list[str]:
    return [p[1] for p in conn.inserts]  # params = (message, dedupe_key, channels)


def test_transition_onset_fires_once_and_keys_on_the_edge() -> None:
    conn = _FakeConn()
    results = [{"check_key": "llm_errors", "status": "fail", "message": "provider down"}]
    counts = emit_transition_alerts(conn, results, {"llm_errors": "ok"}, _RUN_AT)
    assert counts == {"onset": 1, "recovery": 0}
    assert _dedupe_keys(conn) == ["sys:llm_errors:onset:2026-07-09T15:37:00Z"]
    assert conn.inserts[0][0] == "provider down"


def test_transition_ongoing_is_silent() -> None:
    # fail → fail: the incident already alerted at onset; no daily re-alarm.
    conn = _FakeConn()
    results = [{"check_key": "engine_health", "status": "fail", "message": "stalled"}]
    counts = emit_transition_alerts(conn, results, {"engine_health": "fail"}, _RUN_AT)
    assert counts == {"onset": 0, "recovery": 0}
    assert conn.inserts == []


def test_transition_recovery_emits_resolved_row() -> None:
    conn = _FakeConn()
    results = [{"check_key": "llm_errors", "status": "ok", "message": "healthy"}]
    counts = emit_transition_alerts(conn, results, {"llm_errors": "fail"}, _RUN_AT)
    assert counts == {"onset": 0, "recovery": 1}
    assert _dedupe_keys(conn) == ["sys:llm_errors:recovery:2026-07-09T15:37:00Z"]
    assert conn.inserts[0][0].startswith("✓ Recovered")


def test_transition_warn_and_ok_never_ring() -> None:
    conn = _FakeConn()
    results = [
        {"check_key": "street_debt", "status": "warn", "message": "debt rising"},   # ok→warn
        {"check_key": "geo_debt", "status": "ok", "message": "fine"},                # ok→ok
        {"check_key": "merge_latency", "status": "warn", "message": "x"},            # fail? no, prev warn
    ]
    prev = {"street_debt": "ok", "geo_debt": "ok", "merge_latency": "warn"}
    counts = emit_transition_alerts(conn, results, prev, _RUN_AT)
    assert counts == {"onset": 0, "recovery": 0}
    assert conn.inserts == []


def test_transition_new_check_fails_from_absent_baseline() -> None:
    conn = _FakeConn()
    results = [{"check_key": "db_saturation", "status": "fail", "message": "pg_cron timeouts"}]
    counts = emit_transition_alerts(conn, results, {}, _RUN_AT)  # never seen before
    assert counts == {"onset": 1, "recovery": 0}


def test_latest_statuses_reads_distinct_on_latest() -> None:
    conn = _FakeConn(latest_rows=[("llm_errors", "fail"), ("street_debt", "warn")])
    assert latest_statuses(conn) == {"llm_errors": "fail", "street_debt": "warn"}
    sql = conn.executed[0][0]
    assert "DISTINCT ON (check_key)" in sql and "ORDER BY check_key, run_at DESC" in sql


def test_dedupe_key_shape_explicit_day() -> None:
    conn = _FakeConn()
    assert emit_system_alert(conn, "llm_health", "down", day="2026-07-05") is True
    params = conn.insert_params
    # (message, dedupe_key, channels)
    assert params[0] == "down"
    assert params[1] == "sys:llm_health:2026-07-05"


def test_dedupe_key_defaults_to_today_utc() -> None:
    conn = _FakeConn()
    emit_system_alert(conn, "street_debt", "msg")
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    assert conn.insert_params[1] == f"sys:street_debt:{today}"


def test_on_conflict_noop_returns_false() -> None:
    # ON CONFLICT (dedupe_key) DO NOTHING → rowcount 0 → a repeat call is a no-op.
    conn = _FakeConn(insert_rowcount=0)
    assert emit_system_alert(conn, "llm_health", "down", day="2026-07-05") is False


def test_insert_uses_system_health_producer_and_in_app() -> None:
    conn = _FakeConn()
    emit_system_alert(conn, "engine_health", "stuck", day="2026-07-05")
    sql = conn.insert_sql or ""
    assert "'system_health'" in sql and "'system_alert'" in sql and "'in_app'" in sql
    assert "ON CONFLICT (dedupe_key) DO NOTHING" in sql


def test_channels_read_and_passed_as_list() -> None:
    conn = _FakeConn(channels_value=["email", "telegram"])
    emit_system_alert(conn, "llm_health", "down", day="2026-07-05")
    assert conn.insert_params[2] == ["email", "telegram"]


def test_channels_default_empty_when_setting_missing() -> None:
    conn = _FakeConn(channels_value=_MISSING)
    emit_system_alert(conn, "llm_health", "down", day="2026-07-05")
    assert conn.insert_params[2] == []


def test_channels_json_string_is_parsed() -> None:
    conn = _FakeConn(channels_value=json.dumps(["telegram"]))
    emit_system_alert(conn, "llm_health", "down", day="2026-07-05")
    assert conn.insert_params[2] == ["telegram"]


def test_channels_non_list_defaults_empty() -> None:
    conn = _FakeConn(channels_value={"not": "a list"})
    emit_system_alert(conn, "llm_health", "down", day="2026-07-05")
    assert conn.insert_params[2] == []


def test_channels_drops_non_string_entries() -> None:
    conn = _FakeConn(channels_value=["email", 42, None, "telegram"])
    emit_system_alert(conn, "llm_health", "down", day="2026-07-05")
    assert conn.insert_params[2] == ["email", "telegram"]
