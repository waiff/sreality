"""Tests for asset links (toolkit.asset_identity).

Hermetic: a small stateful in-memory fake conn modelling `properties` (id ->
status + asset_id) and `assets`, so the link/unlink branching (new asset, join
existing, UNION across assets, dissolve-on-<2) is genuinely exercised without a
DB. No CI Postgres exists, so a real-DB test is not an option here.
"""

from __future__ import annotations

from typing import Any

import pytest

from toolkit.asset_identity import (
    AssetError,
    get_asset,
    link_properties,
    unlink_property,
)


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        c = self._conn
        p = params or ()
        if "SELECT id, status, asset_id FROM properties WHERE id = ANY" in s:
            self._rows = [
                (i, c.props[i]["status"], c.props[i]["asset_id"])
                for i in p[0]
                if i in c.props
            ]
        elif "INSERT INTO assets" in s and "RETURNING id" in s:
            aid = c.next_asset_id
            c.next_asset_id += 1
            c.assets[aid] = {"status": "active", "note": p[0], "created_by": p[1]}
            self._rows = [(aid,)]
        elif "UPDATE properties SET asset_id = %s WHERE asset_id = %s RETURNING id" in s:
            survivor, other = int(p[0]), int(p[1])
            moved = [i for i, r in c.props.items() if r["asset_id"] == other]
            for i in moved:
                c.props[i]["asset_id"] = survivor
            self._rows = [(i,) for i in sorted(moved)]
        elif "UPDATE assets SET status = 'dissolved'" in s:
            c.assets[int(p[0])]["status"] = "dissolved"
            self._rows = []
        elif "WHERE id = ANY(%s) AND asset_id IS DISTINCT FROM" in s:
            survivor, ids = int(p[0]), p[1]
            moved = [i for i in ids if c.props[i]["asset_id"] != survivor]
            for i in moved:
                c.props[i]["asset_id"] = survivor
            self._rows = [(i,) for i in sorted(moved)]
        elif "INSERT INTO asset_membership_events" in s:
            c.events.append(p)
            self._rows = []
        elif "SELECT id FROM properties WHERE asset_id = %s" in s:
            aid = int(p[0])
            self._rows = [
                (i,) for i in sorted(c.props) if c.props[i]["asset_id"] == aid
            ]
        elif "SELECT asset_id FROM properties WHERE id = %s FOR UPDATE" in s:
            i = int(p[0])
            self._rows = [(c.props[i]["asset_id"],)] if i in c.props else []
        elif "UPDATE properties SET asset_id = NULL WHERE id = %s" in s:
            c.props[int(p[0])]["asset_id"] = None
            self._rows = []
        elif "SELECT id, status, note, created_by, created_at, dissolved_at FROM assets" in s:
            aid = int(p[0])
            a = c.assets.get(aid)
            self._rows = (
                [(aid, a["status"], a.get("note"), a.get("created_by"), None, None)]
                if a
                else []
            )
        else:
            self._rows = []

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, props: dict[int, dict[str, Any]],
                 assets: dict[int, dict[str, Any]] | None = None) -> None:
        self.props = {i: dict(v) for i, v in props.items()}
        self.assets = assets or {}
        self.next_asset_id = (max(self.assets) + 1) if self.assets else 1
        self.events: list[Any] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Ctx:
        return _Ctx()


def _active(*ids: int, asset: int | None = None) -> dict[int, dict[str, Any]]:
    return {i: {"status": "active", "asset_id": asset} for i in ids}


def test_link_two_standalone_creates_asset():
    conn = _FakeConn(_active(1, 2))
    out = link_properties(conn, property_ids=[1, 2])
    aid = out["data"]["asset_id"]
    assert out["data"]["member_property_ids"] == [1, 2]
    assert sorted(out["data"]["newly_linked_property_ids"]) == [1, 2]
    assert conn.props[1]["asset_id"] == aid and conn.props[2]["asset_id"] == aid
    # one 'linked' event per newly attached property
    linked = [e for e in conn.events if e[2] == "linked"]
    assert len(linked) == 2


def test_link_joins_existing_asset():
    # property 1 already in asset 5; linking 1+2 attaches only 2.
    conn = _FakeConn({1: {"status": "active", "asset_id": 5},
                      2: {"status": "active", "asset_id": None}},
                     assets={5: {"status": "active"}})
    out = link_properties(conn, property_ids=[1, 2])
    assert out["data"]["asset_id"] == 5
    assert out["data"]["newly_linked_property_ids"] == [2]
    assert conn.props[2]["asset_id"] == 5


def test_link_unions_two_assets_dissolving_higher():
    # 1 in asset 5, 2 in asset 9 (+ a sibling 3 in 9). Linking 1+2 folds 9 -> 5.
    conn = _FakeConn({1: {"status": "active", "asset_id": 5},
                      2: {"status": "active", "asset_id": 9},
                      3: {"status": "active", "asset_id": 9}},
                     assets={5: {"status": "active"}, 9: {"status": "active"}})
    out = link_properties(conn, property_ids=[1, 2])
    assert out["data"]["asset_id"] == 5
    assert out["data"]["dissolved_asset_ids"] == [9]
    assert out["data"]["member_property_ids"] == [1, 2, 3]  # 3 came along
    assert conn.assets[9]["status"] == "dissolved"
    assert conn.props[3]["asset_id"] == 5


def test_link_needs_two_distinct():
    conn = _FakeConn(_active(1))
    with pytest.raises(AssetError):
        link_properties(conn, property_ids=[1, 1])


def test_link_rejects_missing_or_inactive():
    conn = _FakeConn({1: {"status": "active", "asset_id": None},
                      2: {"status": "merged_away", "asset_id": None}})
    with pytest.raises(AssetError):
        link_properties(conn, property_ids=[1, 2])
    with pytest.raises(AssetError):
        link_properties(conn, property_ids=[1, 999])


def test_unlink_dissolves_when_one_remains():
    # asset 5 has exactly {1,2}; unlinking 1 leaves 1 member -> dissolve.
    conn = _FakeConn({1: {"status": "active", "asset_id": 5},
                      2: {"status": "active", "asset_id": 5}},
                     assets={5: {"status": "active"}})
    out = unlink_property(conn, property_id=1)
    assert out["data"]["asset_dissolved"] is True
    assert out["data"]["remaining_member_ids"] == []
    assert conn.props[1]["asset_id"] is None and conn.props[2]["asset_id"] is None
    assert conn.assets[5]["status"] == "dissolved"


def test_unlink_keeps_asset_with_two_plus():
    conn = _FakeConn({1: {"status": "active", "asset_id": 5},
                      2: {"status": "active", "asset_id": 5},
                      3: {"status": "active", "asset_id": 5}},
                     assets={5: {"status": "active"}})
    out = unlink_property(conn, property_id=1)
    assert out["data"]["asset_dissolved"] is False
    assert out["data"]["remaining_member_ids"] == [2, 3]
    assert conn.assets[5]["status"] == "active"


def test_unlink_property_not_in_asset_raises():
    conn = _FakeConn(_active(1))
    with pytest.raises(AssetError):
        unlink_property(conn, property_id=1)


def test_get_asset_returns_members_or_none():
    conn = _FakeConn({1: {"status": "active", "asset_id": 5},
                      2: {"status": "active", "asset_id": 5}},
                     assets={5: {"status": "active", "note": "mixed-use"}})
    out = get_asset(conn, 5)
    assert out is not None
    assert out["data"]["member_property_ids"] == [1, 2]
    assert out["data"]["status"] == "active"
    assert get_asset(conn, 404) is None
