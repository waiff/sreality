"""clip_room_grouping keys the CLIP room read on the surrogate images.listing_id.

Hermetic recording conn — no DB. The image join must use images.listing_id (the
NOT-NULL FK) so it keeps working once Gate 2 makes sreality_id NULL for non-sreality
portals; sreality_id remains a legacy fallback for the not-yet-repointed engine caller.
"""

from __future__ import annotations

from typing import Any

import pytest

from toolkit.clip_dedup import clip_room_grouping


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
    conn = _FakeConn([("kitchen", 11), ("kitchen", 12), ("bathroom", 13)])
    out = clip_room_grouping(conn, listing_id=500, model="m")
    sql, params = conn.executed[0]
    assert "i.listing_id = %s" in sql
    assert "i.sreality_id" not in sql
    assert params == (500, "m")
    assert out == {"kitchen": [11, 12], "bathroom": [13]}


def test_legacy_sreality_fallback_keys_on_sreality_id() -> None:
    conn = _FakeConn([("kitchen", 1)])
    clip_room_grouping(conn, sreality_id=999, model="m")
    sql, params = conn.executed[0]
    assert "i.sreality_id = %s" in sql
    assert params == (999, "m")


def test_prefers_listing_id_when_both_given() -> None:
    conn = _FakeConn([("kitchen", 1)])
    clip_room_grouping(conn, listing_id=500, sreality_id=999, model="m")
    sql, params = conn.executed[0]
    assert "i.listing_id = %s" in sql
    assert params == (500, "m")


def test_requires_a_key() -> None:
    with pytest.raises(ValueError):
        clip_room_grouping(_FakeConn([]), model="m")


def test_none_when_no_tagged_images() -> None:
    assert clip_room_grouping(_FakeConn([]), listing_id=1, model="m") is None
