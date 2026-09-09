"""`location_v2.<feature>` flags read OFF until an operator writes the row, and an undeclared
feature name fails loud rather than reading a nonexistent key as OFF.

Hermetic fake conn records the executed SQL + params, same shape as
test_payload_churn_write's — the assertion is what SQL a read issues, not a DB round-trip.
"""

from __future__ import annotations

from typing import Any

import pytest

from location_data import serving_flags


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((" ".join(sql.split()), params))

    def fetchone(self) -> tuple[Any, ...] | None:
        return None if self._conn.value is _ABSENT else (self._conn.value,)


_ABSENT = object()


class _FakeConn:
    def __init__(self, value: Any = _ABSENT) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.value = value

    def cursor(self) -> _Cur:
        return _Cur(self)


def test_declared_features_are_the_w6_rollout_order() -> None:
    # The roadmap's W6 row spells the cutover order; the flag list IS that order.
    assert serving_flags.FEATURES == ("dashboards", "dedup", "filters", "map", "estimation")


def test_flag_key_is_namespaced_under_location_v2() -> None:
    assert serving_flags.flag_key("map") == "location_v2.map"


def test_flag_key_refuses_an_undeclared_feature() -> None:
    with pytest.raises(ValueError, match="unknown location_v2 feature 'browse'"):
        serving_flags.flag_key("browse")


def test_missing_row_reads_off() -> None:
    conn = _FakeConn()
    assert serving_flags.is_enabled(conn, "dedup") is False
    assert conn.executed == [
        ("SELECT value FROM app_settings WHERE key = %s", ("location_v2.dedup",)),
    ]


def test_null_value_reads_off() -> None:
    assert serving_flags.is_enabled(_FakeConn(None), "filters") is False


@pytest.mark.parametrize("value", [True, "true", "TRUE", "1", "yes", "on"])
def test_truthy_jsonb_and_text_spellings_read_on(value: Any) -> None:
    assert serving_flags.is_enabled(_FakeConn(value), "map") is True


@pytest.mark.parametrize("value", [False, "false", "0", "off", ""])
def test_falsy_spellings_read_off(value: Any) -> None:
    assert serving_flags.is_enabled(_FakeConn(value), "estimation") is False


def test_is_enabled_never_queries_for_an_undeclared_feature() -> None:
    conn = _FakeConn(True)
    with pytest.raises(ValueError):
        serving_flags.is_enabled(conn, "watchdog")
    assert conn.executed == []
