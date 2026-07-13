"""Tests for the Decision-history feed (api.property_dedup.list_pair_audit): the
property-scope filter, the per-pair feedback join, and the flag filter.
Hermetic: a scripted fake conn, no DB.
"""

from __future__ import annotations

from typing import Any

import api.property_dedup as dedup


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
        self._conn.executed.append((s, params))
        if "count(*) FROM dedup_pair_audit" in s:
            self._rows = [(self._conn.total,)]
        elif "FROM dedup_pair_audit a" in s:
            self._rows = list(self._conn.page_rows)
        else:
            self._rows = []

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, *, total: int = 0, page_rows=None) -> None:
        self.total = total
        self.page_rows = page_rows or []
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)


def _audit_row(
    *, detail: dict | None = None, feedback: tuple[Any, Any, Any, Any] | None = None,
) -> tuple[Any, ...]:
    # 17 columns matching list_pair_audit's SELECT:
    # id, run_at, left_sid, right_sid, left_pid, right_pid, category, stage, outcome,
    # source, merge_group_id, detail, fully_undone, is_incorrect, expected, note, updated.
    fb = feedback or (None, None, None, None)
    return (
        99, "2026-06-25T00:00:00Z", -5, 42, 10, 11, "byt", "phash", "merged",
        "engine", "abc-123", detail if detail is not None else {"phash_pairs": 3},
        False, *fb,
    )


def test_property_id_scopes_both_count_and_page_by_sreality_id() -> None:
    conn = _FakeConn(total=1, page_rows=[_audit_row()])
    out = dedup.list_pair_audit(conn, property_id=335901, outcome="merged")
    sqls = [s for s, _ in conn.executed]
    assert len(sqls) == 2
    for s in sqls:
        assert "a.left_sreality_id IN" in s
        assert "a.right_sreality_id IN" in s
        assert "FROM listings WHERE property_id = %(audit_pid)s" in s
    for _, params in conn.executed:
        assert params["audit_pid"] == 335901
        assert params["outcome"] == "merged"
    assert out["total"] == 1
    assert out["returned"] == 1
    assert out["data"][0]["audit_id"] == 99
    assert out["data"][0]["left_sreality_id"] == -5
    assert out["data"][0]["undone"] is False


def test_no_property_id_omits_the_scope_clause() -> None:
    conn = _FakeConn(total=0, page_rows=[])
    dedup.list_pair_audit(conn)
    for s, params in conn.executed:
        assert "audit_pid" not in s
        assert "audit_pid" not in (params or {})


def test_feedback_join_present_and_unflagged_row_has_null_feedback() -> None:
    conn = _FakeConn(total=1, page_rows=[_audit_row()])
    out = dedup.list_pair_audit(conn)
    # The property-pair-keyed feedback join is on BOTH queries (so flagged-only filters
    # correctly), keyed on the audit row's snapshotted property pair (not the repr listing).
    for s, _ in conn.executed:
        assert "LEFT JOIN dedup_decision_feedback f" in s
        assert "least(a.left_property_id, a.right_property_id)" in s
    assert out["data"][0]["feedback"] is None


def test_feedback_surfaced_and_breakdown_computed() -> None:
    row = _audit_row(
        detail={"stage": "phash", "reason": "image_phash",
                "phash_pairs": 2, "phash_min_pairs": 2, "phash_threshold": 6},
        feedback=(True, "should_dismiss", "wrong merge", "2026-06-26T00:00:00Z"),
    )
    conn = _FakeConn(total=1, page_rows=[row])
    out = dedup.list_pair_audit(conn)
    fb = out["data"][0]["feedback"]
    assert fb == {
        "is_incorrect": True, "expected_outcome": "should_dismiss",
        "note": "wrong merge", "updated_at": "2026-06-26T00:00:00Z",
    }
    # The auditability breakdown is computed from `detail`, not stored.
    keys = [r["key"] for r in out["data"][0]["audit_breakdown"]]
    assert "phash" in keys


def test_flagged_filter_adds_the_is_incorrect_clause() -> None:
    conn = _FakeConn(total=0, page_rows=[])
    dedup.list_pair_audit(conn, flagged=True)
    for s, _ in conn.executed:
        assert "f.is_incorrect IS TRUE" in s
    # flagged falsy must NOT add the clause.
    conn2 = _FakeConn(total=0, page_rows=[])
    dedup.list_pair_audit(conn2, flagged=None)
    for s, _ in conn2.executed:
        assert "f.is_incorrect IS TRUE" not in s


def test_read_path_resolves_self_paired_rows_from_the_merge_ledger() -> None:
    # A legacy self-paired row (left_sreality_id == right_sreality_id, the display
    # bug) must be repaired at read time from the property_merge_events ledger, not
    # shown as one listing id twice. Guardrail: a refactor can't silently drop it.
    conn = _FakeConn(total=1, page_rows=[_audit_row()])
    dedup.list_pair_audit(conn)
    page_sql = next(s for s, _ in conn.executed if "ORDER BY a.run_at DESC" in s)
    assert "CASE WHEN a.left_sreality_id = a.right_sreality_id" in page_sql
    assert "property_merge_events" in page_sql
    assert "survivor_property_id" in page_sql
    assert "array_agg(listing_id)" in page_sql


class _RecCur:
    """Cursor for _record_operator_decision: canned rows keyed by SQL shape."""

    def __init__(self, conn: "_RecConn") -> None:
        self._c = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_RecCur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._c.executed.append((s, params))
        if "repr_listing_id FROM properties" in s:
            # Both properties share the SAME repr_listing_id — the post-merge drift
            # that produced the self-paired bug.
            self._rows = [(10, 555, "byt"), (11, 555, "byt")]
        elif "array_agg(listing_id)" in s:
            # Ledger resolves the two sides to DISTINCT listings.
            self._rows = [(100, 200)]
        else:
            self._rows = []

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _RecConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _RecCur:
        return _RecCur(self)


def _insert_params(conn: _RecConn) -> tuple[Any, ...]:
    return next(p for s, p in conn.executed if "INSERT INTO dedup_pair_audit" in s)


def test_operator_merge_records_ledger_resolved_distinct_listing_ids() -> None:
    conn = _RecConn()
    dedup._record_operator_decision(
        conn, left_property_id=10, right_property_id=11,
        outcome="merged", markers={"stage": "operator"}, merge_group_id="grp-1",
    )
    # The ledger query ran, and the INSERT used its DISTINCT ids (100, 200) — NOT
    # the equal repr_listing_id (555, 555) the properties table returned.
    assert any("array_agg(listing_id)" in s for s, _ in conn.executed)
    ins = _insert_params(conn)
    assert ins[0] == 100 and ins[1] == 200


def test_operator_dismiss_does_not_touch_the_ledger() -> None:
    # A dismissal is not a merge (no group, no re-point) so repr_listing_id is
    # correct and the ledger must not be consulted.
    conn = _RecConn()
    dedup._record_operator_decision(
        conn, left_property_id=10, right_property_id=11,
        outcome="dismissed", markers={"stage": "visual"}, merge_group_id=None,
    )
    assert not any("array_agg(listing_id)" in s for s, _ in conn.executed)
