"""api.labeling border-case writes (migration 310): idempotent flag, read-back
on a repeat, delete. Hermetic fake conn — no DB.

Border cases stay separate from the per-tag `excluded` state (migration 442,
docs/design/tag-annotation-matrix.md): different grain — whole-image quarantine
vs. one ambiguous tag on an otherwise fine image. The tri-state matrix itself is
covered by tests/toolkit/test_tag_annotations.py."""

from __future__ import annotations

from typing import Any

import api.labeling as labeling


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
        if s.startswith("INSERT INTO image_border_cases"):
            # ON CONFLICT DO NOTHING RETURNING created_at — no row back means it
            # already existed (the caller falls back to a SELECT).
            self._row = None if self._conn.border_case_already_exists else ("2026-07-17T00:00:00Z",)
        elif s.startswith("SELECT created_at FROM image_border_cases"):
            self._row = ("2026-07-16T00:00:00Z",)  # the PRE-EXISTING row's timestamp
        elif s.startswith("DELETE FROM image_border_cases"):
            self.rowcount = self._conn.delete_count
            self._row = None
        else:
            raise AssertionError(f"unhandled SQL in fake conn: {s}")

    def fetchone(self) -> Any:
        return self._row


class _FakeConn:
    def __init__(self, *, delete_count: int = 1, border_case_already_exists: bool = False) -> None:
        self.border_case_already_exists = border_case_already_exists
        self.executed: list[tuple[str, Any]] = []
        self.delete_count = delete_count

    def cursor(self) -> _Cur:
        return _Cur(self)


def test_set_border_case_inserts_and_returns_the_new_row() -> None:
    conn = _FakeConn()
    out = labeling.set_border_case(conn, image_id=42)
    sql, params = conn.executed[0]
    assert sql.startswith("INSERT INTO image_border_cases")
    assert params == (42, "operator")
    assert out["data"] == {"image_id": 42, "created_at": "2026-07-17T00:00:00Z"}


def test_set_border_case_is_idempotent_on_a_repeat_flag() -> None:
    # Clicking the button twice for the same image must not error or duplicate —
    # it falls back to reading back the pre-existing row's timestamp.
    conn = _FakeConn(border_case_already_exists=True)
    out = labeling.set_border_case(conn, image_id=42)
    assert len(conn.executed) == 2  # the no-op INSERT, then the fallback SELECT
    assert out["data"] == {"image_id": 42, "created_at": "2026-07-16T00:00:00Z"}


def test_delete_border_case_reports_deleted() -> None:
    conn = _FakeConn(delete_count=1)
    out = labeling.delete_border_case(conn, image_id=42)
    _, params = conn.executed[0]
    assert params == (42,)
    assert out["data"]["deleted"] is True

    conn2 = _FakeConn(delete_count=0)
    out2 = labeling.delete_border_case(conn2, image_id=1)
    assert out2["data"]["deleted"] is False
