"""Tests for the property-scoped Decision-history filter (the listing-detail
"merge decisions" link → api.property_dedup.list_pair_audit(property_id=...)).
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


def _audit_row() -> tuple[Any, ...]:
    # 12 columns matching list_pair_audit's SELECT (last = fully_undone).
    return (
        "2026-06-25T00:00:00Z", -5, 42, 10, 11, "byt", "phash", "merged",
        "engine", "abc-123", {"phash_pairs": 3}, False,
    )


def test_property_id_scopes_both_count_and_page_by_sreality_id() -> None:
    conn = _FakeConn(total=1, page_rows=[_audit_row()])
    out = dedup.list_pair_audit(conn, property_id=335901, outcome="merged")
    # The scope keys on the STABLE sreality_id (property_id re-points on merge),
    # via a subquery into listings — and it must apply to BOTH queries.
    sqls = [s for s, _ in conn.executed]
    assert len(sqls) == 2
    for s in sqls:
        assert "a.left_sreality_id IN" in s
        assert "a.right_sreality_id IN" in s
        assert "FROM listings WHERE property_id = %(audit_pid)s" in s
    # The property id is bound, not interpolated.
    for _, params in conn.executed:
        assert params["audit_pid"] == 335901
        assert params["outcome"] == "merged"
    assert out["total"] == 1
    assert out["returned"] == 1
    assert out["data"][0]["left_sreality_id"] == -5
    assert out["data"][0]["undone"] is False


def test_no_property_id_omits_the_scope_clause() -> None:
    conn = _FakeConn(total=0, page_rows=[])
    dedup.list_pair_audit(conn)
    for s, params in conn.executed:
        assert "audit_pid" not in s
        assert "audit_pid" not in (params or {})
