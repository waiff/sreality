"""Tests for scraper.db.upsert_listing_with_property (Slice 0 property linkage).

Hermetic: upsert_listing is stubbed so the only SQL reaching the fake conn
comes from _ensure_singleton_property. A fake cursor records executions and
replays canned fetchone() results.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from scraper import db


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = ()) -> None:
        self._conn.executions.append((" ".join(sql.split()), params))

    def fetchone(self) -> Any:
        return self._conn.fetch_results.popleft()


class _FakeConn:
    def __init__(self, fetch_results: list[Any]) -> None:
        self.executions: list[tuple[str, Any]] = []
        self.fetch_results: deque[Any] = deque(fetch_results)

    def transaction(self) -> _Ctx:
        return _Ctx()

    def cursor(self) -> _Cur:
        return _Cur(self)


def _stub_upsert(monkeypatch, result: str = "new") -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def _fake(_c, row, raw, h):  # noqa: ANN001
        captured["row"] = row
        captured["hash"] = h
        return result

    monkeypatch.setattr(db, "upsert_listing", _fake)
    return captured


def _find(executions, needle: str) -> tuple[str, Any] | None:
    return next((e for e in executions if needle in e[0]), None)


def test_new_listing_creates_and_links_property(monkeypatch):
    captured = _stub_upsert(monkeypatch, "new")
    # SELECT property_id -> None (unlinked); INSERT ... RETURNING id -> 42
    conn = _FakeConn([(None,), (42,)])

    result = db.upsert_listing_with_property(
        conn, {"sreality_id": 555}, {}, "h" * 8
    )

    assert result == "new"
    assert captured["hash"] == "h" * 8
    assert _find(conn.executions, "INSERT INTO properties") is not None
    link = _find(conn.executions, "SET property_id =")
    assert link is not None and link[1] == (42, 555)
    assert _find(conn.executions, "source_id_native = sreality_id::text") is not None
    # A linked listing would be refreshed, not inserted:
    assert _find(conn.executions, "UPDATE properties p") is None


def test_linked_listing_refreshes_property(monkeypatch):
    _stub_upsert(monkeypatch, "updated")
    # SELECT property_id -> 7 (already linked)
    conn = _FakeConn([(7,)])

    result = db.upsert_listing_with_property(
        conn, {"sreality_id": 777}, {}, "abc"
    )

    assert result == "updated"
    assert _find(conn.executions, "UPDATE properties p") is not None
    assert _find(conn.executions, "INSERT INTO properties") is None
