"""api.property_dedup.phash_audit: the /phash-audit range browse over dedup_pair_audit
pairs. Hermetic fake conn — no DB."""

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
            self._rows = [(self._conn.scanned,)]
        elif "WITH scoped AS" in s:
            self._rows = list(self._conn.join_rows)
        else:
            self._rows = []

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, *, scanned: int = 0, join_rows=None) -> None:
        self.scanned = scanned
        self.join_rows = join_rows or []
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)


def _join_row() -> tuple[Any, ...]:
    # 23 columns matching phash_audit's join SELECT.
    return (
        99, -5, 42, 10, 11, "merged", "byt", "2026-07-01T00:00:00Z",
        1001, "https://x/a.jpg", None, "kitchen", "kitchen", 0.91, None,
        2002, "https://x/b.jpg", None, "kitchen", "kitchen", 0.88, None,
        9,
    )


def test_hamming_range_passed_through_and_result_shaped() -> None:
    conn = _FakeConn(scanned=3, join_rows=[_join_row()])
    out = dedup.phash_audit(conn, hamming_min=7, hamming_max=15)
    join_sql, join_params = next(
        (s, p) for s, p in conn.executed if "WITH scoped AS" in s
    )
    assert join_params["hmin"] == 7 and join_params["hmax"] == 15
    assert "BETWEEN %(hmin)s AND %(hmax)s" in join_sql
    row = out["data"][0]
    assert row["audit_id"] == 99
    assert row["hamming"] == 9
    assert row["left_property_id"] == 10 and row["right_property_id"] == 11
    assert row["left_image"] == {
        "image_id": 1001, "sreality_url": "https://x/a.jpg",
        "storage_path": None, "room_type": "kitchen",
        "fine_tag": "kitchen", "confidence": 0.91, "render_score": None,
    }
    assert out["scanned_pairs"] == 3
    assert out["returned"] == 1


def test_category_main_and_outcome_scope_both_queries() -> None:
    conn = _FakeConn(scanned=0, join_rows=[])
    dedup.phash_audit(
        conn, hamming_min=0, hamming_max=15, category_main="dum", outcome="dismissed",
    )
    for s, params in conn.executed:
        assert "a.category_main = %(category_main)s" in s
        assert "a.outcome = %(outcome)s" in s
        assert params["category_main"] == "dum"
        assert params["outcome"] == "dismissed"


def test_no_scope_filters_omit_the_where_clause() -> None:
    conn = _FakeConn(scanned=0, join_rows=[])
    dedup.phash_audit(conn, hamming_min=0, hamming_max=15)
    for s, params in conn.executed:
        assert "category_main" not in (params or {})
        assert "outcome" not in (params or {})


def test_room_type_matches_either_side() -> None:
    conn = _FakeConn(scanned=0, join_rows=[])
    dedup.phash_audit(conn, hamming_min=0, hamming_max=15, room_type="floor_plan")
    join_sql, join_params = next(
        (s, p) for s, p in conn.executed if "WITH scoped AS" in s
    )
    assert "ta.logical_tag = %(room_type)s OR tb.logical_tag = %(room_type)s" in join_sql
    assert join_params["room_type"] == "floor_plan"


def test_scan_cap_bounds_the_scoped_population() -> None:
    conn = _FakeConn(scanned=0, join_rows=[])
    out = dedup.phash_audit(conn, hamming_min=0, hamming_max=15)
    join_sql, join_params = next(
        (s, p) for s, p in conn.executed if "WITH scoped AS" in s
    )
    assert "LIMIT %(cap)s" in join_sql
    assert join_params["cap"] == out["scan_cap"]
