"""Tests for cluster merge/dismiss (api.property_dedup.merge_cluster / dismiss_cluster).

A cluster is several pairwise candidates that connect the same property (A-B,
B-C, A-C). merge_cluster picks the single oldest property as survivor and merges
every other into it under ONE merge_group_id. Hermetic: a scripted fake conn +
a stubbed merge_properties so we assert the survivor/retired choice and that the
whole cluster is resolved, without a DB.
"""

from __future__ import annotations

from typing import Any

import pytest

import api.property_dedup as dedup
from toolkit.property_identity import MergeError


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if "FROM property_identity_candidates WHERE id = ANY" in s:
            self._rows = list(self._conn.candidate_rows)
        elif "FROM properties WHERE id = ANY" in s and "status = 'active'" in s:
            # oldest active survivor: first by first_seen/id — the script gives it
            self._rows = [(self._conn.survivor,)]
        elif "UPDATE property_identity_candidates" in s:
            self._conn.marked.append(params)
            self._rows = []
        else:
            self._rows = []

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, candidate_rows: list[tuple[Any, ...]], survivor: int) -> None:
        self.candidate_rows = candidate_rows
        self.survivor = survivor
        self.executed: list[tuple[str, Any]] = []
        self.marked: list[Any] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Ctx:
        return _Ctx()


def _stub_merge(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_merge(conn, *, survivor_id, retired_id, reason, source,
                   confidence=None, markers=None, merge_group_id=None):
        calls.append({"survivor": survivor_id, "retired": retired_id,
                      "group": merge_group_id, "reason": reason})
        group = merge_group_id or "grp-1"
        return {"data": {"merge_group_id": group, "listings_moved": 2}}

    monkeypatch.setattr(dedup, "merge_properties", fake_merge)
    return calls


def test_merge_cluster_three_members_one_survivor(monkeypatch):
    """A-B, B-C → properties {1,2,3}; survivor=1; 2 and 3 merge into 1 in one group."""
    calls = _stub_merge(monkeypatch)
    # candidate rows: (id, left, right, status)
    rows = [(10, 1, 2, "proposed"), (11, 2, 3, "proposed")]
    conn = _FakeConn(rows, survivor=1)

    result = dedup.merge_cluster(conn, [10, 11])

    assert result is not None
    assert result["survivor_id"] == 1
    assert result["retired_ids"] == [2, 3]
    assert result["candidates_resolved"] == 2
    # two merges, both into survivor 1, both under the same group
    assert [c["retired"] for c in calls] == [2, 3]
    assert all(c["survivor"] == 1 for c in calls)
    assert calls[0]["group"] is None        # first call seeds the group
    assert calls[1]["group"] == "grp-1"     # second reuses it
    # every candidate row marked merged
    assert conn.marked and conn.marked[-1][1] == [10, 11]


def test_merge_cluster_rejects_non_proposed(monkeypatch):
    _stub_merge(monkeypatch)
    rows = [(10, 1, 2, "merged")]
    conn = _FakeConn(rows, survivor=1)
    with pytest.raises(MergeError):
        dedup.merge_cluster(conn, [10])


def test_merge_cluster_empty_returns_none(monkeypatch):
    _stub_merge(monkeypatch)
    conn = _FakeConn([], survivor=0)
    assert dedup.merge_cluster(conn, []) is None


def test_dismiss_cluster_marks_all(monkeypatch):
    conn = _FakeConn([], survivor=0)

    # dismiss_cluster only runs the UPDATE ... RETURNING; script it to return ids.
    class _DCur(_Cur):
        def execute(self, sql: str, params: Any = None) -> None:
            s = " ".join(sql.split())
            self._conn.executed.append((s, params))
            if "SET status = 'dismissed'" in s:
                self._rows = [(10,), (11,)]
            else:
                self._rows = []

    monkeypatch.setattr(conn, "cursor", lambda: _DCur(conn))
    result = dedup.dismiss_cluster(conn, [10, 11])
    assert result == {"dismissed": [10, 11], "status": "dismissed"}
