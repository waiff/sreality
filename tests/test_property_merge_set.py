"""Tests for the operator subset merge (api.property_merge.merge_property_set).

The operator ticks a set of properties; the oldest ACTIVE one survives and every
other merges into it under ONE reversible group. Hermetic: a scripted fake conn +
a stubbed merge_properties, so we assert the survivor/retired choice and the
atomicity of the loop without a DB.
"""

from __future__ import annotations

from typing import Any

import pytest

import api.property_merge as pm
from toolkit.property_identity import MergeError


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Cur:
    def __init__(self, conn: "_SetConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if "FROM properties WHERE id = ANY" in s and "status = 'active'" in s:
            self._rows = [(pid,) for pid in self._conn.active_ids]
        else:
            self._rows = []

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _SetConn:
    def __init__(self, active_ids: list[int]) -> None:
        self.active_ids = active_ids
        self.executed: list[tuple[str, Any]] = []

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

    monkeypatch.setattr(pm, "merge_properties", fake_merge)
    return calls


def test_merge_property_set_oldest_survives(monkeypatch):
    """Operator ticks properties {7,3,9}; oldest active (3) survives, 7+9 merge in
    under ONE group (the first call seeds it, the rest reuse it)."""
    calls = _stub_merge(monkeypatch)
    # active query returns oldest-first; survivor=3
    conn = _SetConn(active_ids=[3, 7, 9])
    result = pm.merge_property_set(conn, [7, 3, 9])
    assert result is not None
    assert result["survivor_id"] == 3
    assert result["retired_ids"] == [7, 9]
    assert result["merge_group_id"] == "grp-1"
    assert result["listings_moved"] == 4
    assert [c["retired"] for c in calls] == [7, 9]
    assert all(c["survivor"] == 3 for c in calls)
    assert all(c["reason"] == "manual_subset" for c in calls)
    assert calls[0]["group"] is None        # first call seeds the group
    assert calls[1]["group"] == "grp-1"     # second reuses it


def test_merge_property_set_needs_two(monkeypatch):
    _stub_merge(monkeypatch)
    assert pm.merge_property_set(_SetConn(active_ids=[5]), [5]) is None
    # de-dups, so a single distinct id is a no-op
    assert pm.merge_property_set(_SetConn(active_ids=[5]), [5, 5]) is None


def test_merge_property_set_one_active_raises(monkeypatch):
    _stub_merge(monkeypatch)
    with pytest.raises(MergeError):
        # two requested but only one is still active
        pm.merge_property_set(_SetConn(active_ids=[3]), [3, 7])


def test_merge_property_set_touches_no_candidate_table(monkeypatch):
    """The legacy decision layer is gone: a subset merge must never read or write
    property_identity_candidates (dropped) or dedup_pair_audit (frozen)."""
    _stub_merge(monkeypatch)
    conn = _SetConn(active_ids=[3, 7])
    pm.merge_property_set(conn, [3, 7])
    sqls = " ".join(s for s, _ in conn.executed)
    assert "property_identity_candidates" not in sqls
    assert "dedup_pair_audit" not in sqls


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

    monkeypatch.setattr(pm, "merge_properties", fake_merge)
    # survivor=3, retired=[7, 9]; the merge of 9 is refused after 7 succeeded.
    with pytest.raises(MergeError):
        pm.merge_property_set(_RecConn(active_ids=[3, 7, 9]), [3, 7, 9])
    # the merge loop ran inside a transaction that received the exception →
    # a real DB would ROLLBACK the already-applied merge of 7 (no partial merge).
    assert MergeError in exits
