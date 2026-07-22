"""_fetch_images keys the room-classify image read on the surrogate images.listing_id.

Hermetic recording conn — no DB. Keying on images.listing_id (the NOT-NULL FK) keeps the
scheduled dedup engine's classify path working once Gate 2 makes sreality_id NULL for the
non-sreality portals; sreality_id stays a legacy fallback for the engine caller that
hasn't been repointed yet.
"""

from __future__ import annotations

from typing import Any

import pytest

from toolkit.image_classification import ClassifyError, _fetch_images


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((" ".join(sql.split()), params))

    def fetchall(self) -> Any:
        return self._conn.rows


class _FakeConn:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.rows = rows

    def cursor(self) -> _Cur:
        return _Cur(self)


def test_keys_on_listing_id_surrogate() -> None:
    conn = _FakeConn([(11, 0, "a/1.jpg"), (12, 1, "a/2.jpg")])
    out = _fetch_images(conn, 12, listing_id=500)
    sql, params = conn.executed[0]
    assert "WHERE listing_id = %s" in sql
    assert "sreality_id" not in sql
    assert params == (500, 12)
    assert out == [
        {"id": 11, "sequence": 0, "storage_path": "a/1.jpg"},
        {"id": 12, "sequence": 1, "storage_path": "a/2.jpg"},
    ]


def test_legacy_sreality_fallback_keys_on_sreality_id() -> None:
    conn = _FakeConn([(1, 0, "x")])
    _fetch_images(conn, 12, sreality_id=999)
    sql, params = conn.executed[0]
    assert "WHERE sreality_id = %s" in sql
    assert params == (999, 12)


def test_prefers_listing_id_when_both_given() -> None:
    conn = _FakeConn([(1, 0, "x")])
    _fetch_images(conn, 12, listing_id=500, sreality_id=999)
    sql, params = conn.executed[0]
    assert "WHERE listing_id = %s" in sql
    assert params == (500, 12)


def test_requires_a_key() -> None:
    with pytest.raises(ClassifyError):
        _fetch_images(_FakeConn([]), 12)
