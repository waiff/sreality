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

    # dismiss_cluster runs UPDATE ... RETURNING id, left_property_id,
    # right_property_id, markers_matched (4 cols, so each dismissal can be logged to
    # the unified decision audit); the operator-audit SELECT/INSERT fall to else.
    class _DCur(_Cur):
        def execute(self, sql: str, params: Any = None) -> None:
            s = " ".join(sql.split())
            self._conn.executed.append((s, params))
            if "SET status = 'dismissed'" in s:
                self._rows = [(10, 100, 101, {}), (11, 110, 111, {})]
            else:
                self._rows = []

    monkeypatch.setattr(conn, "cursor", lambda: _DCur(conn))
    result = dedup.dismiss_cluster(conn, [10, 11])
    assert result == {"dismissed": [10, 11], "status": "dismissed"}


# --- merge_property_set (operator-checked subset) --------------------------

class _SetCur:
    def __init__(self, conn: "_SetConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_SetCur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if "FROM properties WHERE id = ANY" in s and "status = 'active'" in s:
            self._rows = [(pid,) for pid in self._conn.active_ids]
        elif "FROM property_identity_candidates WHERE status = 'proposed'" in s:
            self._rows = list(self._conn.dangling)
        else:
            self._rows = []

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _SetConn:
    def __init__(self, active_ids: list[int], dangling: list[tuple[Any, ...]] | None = None) -> None:
        self.active_ids = active_ids
        self.dangling = dangling or []
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _SetCur:
        return _SetCur(self)

    def transaction(self) -> _Ctx:
        return _Ctx()


def test_merge_property_set_oldest_survives(monkeypatch):
    """Operator ticks properties {7,3,9}; oldest active (3) survives, 7+9 merge in."""
    calls = _stub_merge(monkeypatch)
    # active query returns oldest-first; survivor=3
    conn = _SetConn(active_ids=[3, 7, 9])
    result = dedup.merge_property_set(conn, [7, 3, 9])
    assert result is not None
    assert result["survivor_id"] == 3
    assert result["retired_ids"] == [7, 9]
    assert [c["retired"] for c in calls] == [7, 9]
    assert all(c["survivor"] == 3 for c in calls)
    assert all(c["reason"] == "manual_subset" for c in calls)


def test_merge_property_set_needs_two(monkeypatch):
    _stub_merge(monkeypatch)
    assert dedup.merge_property_set(_SetConn(active_ids=[5]), [5]) is None
    # de-dups, so a single distinct id is a no-op
    assert dedup.merge_property_set(_SetConn(active_ids=[5]), [5, 5]) is None


def test_merge_property_set_one_active_raises(monkeypatch):
    _stub_merge(monkeypatch)
    with pytest.raises(MergeError):
        # two requested but only one is still active
        dedup.merge_property_set(_SetConn(active_ids=[3]), [3, 7])


def test_merge_property_set_partial_failure_rolls_back(monkeypatch):
    """A refusal on a later pair propagates through the OUTER transaction, so a
    real DB rolls the whole set back instead of committing a partial merge."""
    exits: list[Any] = []

    class _RecCtx:
        def __enter__(self) -> "_RecCtx":
            return self

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            exits.append(exc_type)
            return False  # never suppress — let the error propagate

    class _RecConn(_SetConn):
        def transaction(self) -> _RecCtx:
            return _RecCtx()

    def fake_merge(conn, *, survivor_id, retired_id, **kwargs):
        if retired_id == 9:
            raise MergeError("category_main mismatch (byt vs dum)")
        return {"data": {"merge_group_id": "grp-1", "listings_moved": 1}}

    monkeypatch.setattr(dedup, "merge_properties", fake_merge)
    # survivor=3, retired=[7, 9]; the merge of 9 is refused after 7 succeeded.
    with pytest.raises(MergeError):
        dedup.merge_property_set(_RecConn(active_ids=[3, 7, 9]), [3, 7, 9])
    # the merge loop ran inside a transaction that received the exception →
    # a real DB would ROLLBACK the already-applied merge of 7 (no partial merge).
    assert MergeError in exits
