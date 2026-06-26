"""api.property_dedup decision-feedback writes: canonical pair ordering, validation,
note cleaning, delete. Hermetic fake conn — no DB."""

from __future__ import annotations

from typing import Any

import pytest

import api.property_dedup as dedup


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._row: tuple[Any, ...] | None = None
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if s.startswith("INSERT INTO dedup_decision_feedback"):
            # RETURNING is_incorrect, expected_outcome, note, updated_at
            self._row = (params[2], params[3], params[4], "2026-06-26T00:00:00Z")
        elif s.startswith("DELETE FROM dedup_decision_feedback"):
            self.rowcount = self._conn.delete_count
            self._row = None

    def fetchone(self) -> Any:
        return self._row


class _FakeConn:
    def __init__(self, *, delete_count: int = 1) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.delete_count = delete_count

    def cursor(self) -> _Cur:
        return _Cur(self)


def test_set_feedback_canonicalises_pair_and_cleans_note() -> None:
    conn = _FakeConn()
    out = dedup.set_decision_feedback(
        conn, left_sreality_id=200, right_sreality_id=100,
        expected_outcome="should_dismiss", note="  bad merge  ", category_main="byt",
    )
    _, params = conn.executed[0]
    assert params[0] == 100 and params[1] == 200  # low, high
    assert params[3] == "should_dismiss"
    assert params[4] == "bad merge"  # trimmed
    assert out["data"]["left_sreality_id"] == 100
    assert out["data"]["right_sreality_id"] == 200
    assert out["data"]["expected_outcome"] == "should_dismiss"


def test_set_feedback_blank_note_becomes_null() -> None:
    conn = _FakeConn()
    dedup.set_decision_feedback(
        conn, left_sreality_id=-9, right_sreality_id=-5, note="   ",
    )
    _, params = conn.executed[0]
    assert params[0] == -9 and params[1] == -5  # negatives order numerically
    assert params[4] is None


def test_set_feedback_rejects_bad_expected_outcome() -> None:
    with pytest.raises(ValueError):
        dedup.set_decision_feedback(
            _FakeConn(), left_sreality_id=1, right_sreality_id=2,
            expected_outcome="nonsense",
        )


def test_set_feedback_rejects_identical_pair() -> None:
    with pytest.raises(ValueError):
        dedup.set_decision_feedback(
            _FakeConn(), left_sreality_id=7, right_sreality_id=7,
        )


def test_delete_feedback_canonicalises_and_reports() -> None:
    conn = _FakeConn(delete_count=1)
    out = dedup.delete_decision_feedback(conn, left_sreality_id=200, right_sreality_id=100)
    _, params = conn.executed[0]
    assert params == (100, 200)
    assert out["data"]["deleted"] is True

    conn2 = _FakeConn(delete_count=0)
    out2 = dedup.delete_decision_feedback(conn2, left_sreality_id=1, right_sreality_id=2)
    assert out2["data"]["deleted"] is False
