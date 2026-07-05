"""Hermetic tests for toolkit.system_alerts — no DB, a scripted fake connection."""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from toolkit.system_alerts import emit_system_alert


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
        elif "INSERT INTO notification_dispatches" in sql:
            self._conn.insert_sql = sql
            self._conn.insert_params = params
            self.rowcount = self._conn.insert_rowcount
            return
        self.rowcount = 0

    def fetchone(self) -> Any:
        return self._conn._fetch

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


_MISSING = object()


class _FakeConn:
    def __init__(self, *, channels_value: Any = _MISSING, insert_rowcount: int = 1) -> None:
        self.channels_value = channels_value
        self.insert_rowcount = insert_rowcount
        self.executed: list[tuple[str, Any]] = []
        self.insert_sql: str | None = None
        self.insert_params: Any = None
        self._fetch: Any = None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


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
