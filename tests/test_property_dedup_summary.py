"""Tests for the dedup review-backlog visibility surface:
api.property_dedup.summary + list_candidates' real total + the reason/verdict
filter builder. Hermetic: a scripted fake conn, no DB.
"""

from __future__ import annotations

from typing import Any

import api.property_dedup as dedup
from api.location_filter import DistrictChip
from api.property_dedup import LEGACY_REASON, NULL_VERDICT, _candidate_filters


# --- _candidate_filters (pure) ---------------------------------------------

def test_filters_reason_and_verdict_bind_params() -> None:
    where, params = _candidate_filters("proposed", None, "no_images", None)
    assert "c.status = %(status)s" in where
    assert "c.markers_matched->>'reason' = %(reason)s" in where
    assert params == {"status": "proposed", "reason": "no_images"}


def test_filters_legacy_reason_is_null() -> None:
    where, params = _candidate_filters("proposed", None, LEGACY_REASON, None)
    assert "c.markers_matched->>'reason' IS NULL" in where
    assert "reason" not in params  # sentinel, not a bound value


def test_filters_null_verdict_sentinel_is_null() -> None:
    where, params = _candidate_filters("proposed", None, "visual_inconclusive", NULL_VERDICT)
    assert "c.markers_matched->>'verdict' IS NULL" in where
    assert "verdict" not in params


def test_filters_concrete_verdict_binds() -> None:
    where, params = _candidate_filters("proposed", None, "visual_inconclusive", "Low")
    assert "c.markers_matched->>'verdict' = %(verdict)s" in where
    assert params["verdict"] == "Low"


def test_filters_districts_matches_either_side_of_pair() -> None:
    # A pair matches on EITHER candidate property (l/r) touching the place --
    # the operator is prioritising review by location, not asserting the
    # engine already agrees the pair is in one place.
    where, params = _candidate_filters(
        "proposed", None, None, None,
        districts=[DistrictChip(name="Jihlava", level="obec", id=586846)],
    )
    assert "l.obec_id = %(district_id_l_0)s" in where
    assert "r.obec_id = %(district_id_r_0)s" in where
    assert params["district_id_l_0"] == 586846
    assert params["district_id_r_0"] == 586846


def test_filters_no_districts_omits_location_clause() -> None:
    where, params = _candidate_filters("proposed", None, None, None, districts=None)
    assert "obec_id" not in where
    assert not any(k.startswith("district_") for k in params)


def test_filters_category_main_matches_either_side_of_pair() -> None:
    # A pair CAN legitimately span two types (the sanctioned dům<->komercni
    # cross-type merge, rule #15) — the Type tab must match if EITHER side is
    # the picked category, not assert both sides already agree.
    where, params = _candidate_filters(
        "proposed", None, None, None, category_main="komercni",
    )
    assert "(l.category_main = %(category_main)s OR r.category_main = %(category_main)s)" in where
    assert params["category_main"] == "komercni"


def test_filters_no_category_main_omits_the_clause() -> None:
    where, params = _candidate_filters("proposed", None, None, None, category_main=None)
    assert "category_main" not in where
    assert "category_main" not in params


# --- fake conn --------------------------------------------------------------

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
        if "count(*) FROM property_identity_candidates c" in s:
            self._rows = [(self._conn.total,)]
        elif "GROUP BY 1, 2" in s:
            self._rows = list(self._conn.bucket_rows)
        elif "ORDER BY c.created_at DESC" in s:
            self._rows = list(self._conn.page_rows)
        else:
            self._rows = []

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, *, total: int = 0, page_rows=None, bucket_rows=None) -> None:
        self.total = total
        self.page_rows = page_rows or []
        self.bucket_rows = bucket_rows or []
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)


def _candidate_row(cid: int) -> tuple[Any, ...]:
    # 15 columns matching list_candidates' SELECT (last 4 = LEFT-joined feedback:
    # is_incorrect, expected_outcome, note, updated_at — NULL for an unflagged pair).
    return (cid, "street_disposition", "proposed", 0.6, {"reason": "no_images"},
            False, None, "2026-06-18T00:00:00Z", None, {"property_id": 1}, {"property_id": 2},
            None, None, None, None)


def test_list_candidates_total_is_real_count_not_page_size() -> None:
    # 2 rows on the page, but the COUNT says 8178 — the UI must see the true total.
    conn = _FakeConn(total=8178, page_rows=[_candidate_row(1), _candidate_row(2)])
    out = dedup.list_candidates(conn, status="proposed", limit=2)
    assert out["total"] == 8178
    assert out["returned"] == 2
    assert len(out["data"]) == 2


def test_list_candidates_applies_reason_filter() -> None:
    conn = _FakeConn(total=15, page_rows=[_candidate_row(1)])
    dedup.list_candidates(conn, status="proposed", reason="visual_inconclusive", verdict="Low")
    # both the COUNT and the page query carry the reason+verdict clauses
    sqls = [s for s, _ in conn.executed]
    assert any("count(*)" in s and "reason" in s and "verdict" in s for s in sqls)
    assert any("ORDER BY c.created_at DESC" in s and "verdict" in s for s in sqls)


def test_list_candidates_count_and_page_share_the_same_properties_join() -> None:
    # Regression: the COUNT query used to omit the `l`/`r` properties join the
    # page SELECT has, so a filter referencing `l.`/`r.` (districts, category_main)
    # raised `UndefinedTable: missing FROM-clause entry for table "l"` on the COUNT
    # only. Both queries now share one `_CANDIDATES_FROM` — assert they can never
    # diverge again, with EVERY filter that touches l/r active at once.
    conn = _FakeConn(total=3, page_rows=[_candidate_row(1)])
    dedup.list_candidates(
        conn, status="proposed", category_main="komercni",
        districts=[DistrictChip(name="Jihlava", level="obec", id=586846)],
    )
    sqls = [s for s, _ in conn.executed]
    assert len(sqls) == 2
    for s in sqls:
        assert "JOIN properties l ON l.id = c.left_property_id" in s
        assert "JOIN properties r ON r.id = c.right_property_id" in s
        assert "l.category_main = %(category_main)s OR r.category_main = %(category_main)s" in s
        assert "l.obec_id = %(district_id_l_0)s" in s
        assert "r.obec_id = %(district_id_r_0)s" in s


def test_list_candidates_applies_category_main_filter() -> None:
    conn = _FakeConn(total=4, page_rows=[_candidate_row(1)])
    dedup.list_candidates(conn, status="proposed", category_main="byt")
    sqls = [s for s, _ in conn.executed]
    assert all("category_main" in s for s in sqls)
    assert all(p["category_main"] == "byt" for _, p in conn.executed)


def test_summary_buckets_and_total() -> None:
    conn = _FakeConn(bucket_rows=[
        ("no_images", None, 2946),
        (LEGACY_REASON, None, 3958),
        ("visual_inconclusive", "Low", 15),
    ])
    out = dedup.summary(conn)["data"]
    assert out["status"] == "proposed"
    assert out["total"] == 2946 + 3958 + 15
    assert {"reason": "visual_inconclusive", "verdict": "Low", "count": 15} in out["buckets"]
    assert out["buckets"][0]["count"] == 2946  # ordered by n desc (as scripted)
