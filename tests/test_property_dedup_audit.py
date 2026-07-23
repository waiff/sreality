"""Tests for the Decision-history feed (api.property_dedup.list_pair_audit): the
property-scope filter, the per-pair feedback join, and the flag filter.
Hermetic: a scripted fake conn, no DB.
"""

from __future__ import annotations

from typing import Any

import api.property_dedup as dedup
from api.location_filter import DistrictChip


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


def test_property_id_scopes_both_count_and_page_by_surrogate_listing_id() -> None:
    # Scoped via left/right_listing_id (surrogate, always populated) -- NOT
    # sreality_id, which is NULL for a post-Gate-2 non-sreality child listing and
    # would silently drop it from the property's decision history.
    conn = _FakeConn(total=1, page_rows=[_audit_row()])
    out = dedup.list_pair_audit(conn, property_id=335901, outcome="merged")
    sqls = [s for s, _ in conn.executed]
    assert len(sqls) == 2
    for s in sqls:
        assert "a.left_listing_id IN" in s
        assert "a.right_listing_id IN" in s
        assert "(SELECT id FROM listings WHERE property_id = %(audit_pid)s)" in s
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


def test_districts_join_properties_and_match_either_side() -> None:
    conn = _FakeConn(total=0, page_rows=[])
    dedup.list_pair_audit(
        conn, districts=[DistrictChip(name="Jihlava", level="obec", id=586846)],
    )
    for s, params in conn.executed:
        assert "LEFT JOIN properties pl ON pl.id = a.left_property_id" in s
        assert "LEFT JOIN properties pr ON pr.id = a.right_property_id" in s
        assert "pl.obec_id = %(district_id_pl_0)s" in s
        assert "pr.obec_id = %(district_id_pr_0)s" in s
        assert params["district_id_pl_0"] == 586846
        assert params["district_id_pr_0"] == 586846


def test_no_districts_omits_the_properties_join() -> None:
    conn = _FakeConn(total=0, page_rows=[])
    dedup.list_pair_audit(conn)
    for s, params in conn.executed:
        assert "LEFT JOIN properties pl" not in s
        assert "LEFT JOIN properties pr" not in s
        assert not any(k.startswith("district_") for k in (params or {}))


def test_category_main_matches_either_side_not_the_stamped_column() -> None:
    # dedup_pair_audit.category_main is the ENGINE's single stamped classification
    # for the whole pair (falls back to whichever side is non-NULL) — a sanctioned
    # dům<->komercni cross-type merge can be stamped with only ONE of the two
    # types. Filtering the pair's own two `properties` rows instead (not `a.category_main`)
    # is what lets it surface under both type tabs.
    conn = _FakeConn(total=0, page_rows=[])
    dedup.list_pair_audit(conn, category_main="komercni")
    for s, params in conn.executed:
        assert "a.category_main = %(category_main)s" not in s
        assert "LEFT JOIN properties pl ON pl.id = a.left_property_id" in s
        assert "LEFT JOIN properties pr ON pr.id = a.right_property_id" in s
        assert "(pl.category_main = %(category_main)s OR pr.category_main = %(category_main)s)" in s
        assert params["category_main"] == "komercni"


def test_category_main_and_districts_share_one_properties_join() -> None:
    # Both per-side filters need the same pl/pr join — it must appear exactly
    # once even when both filters are set together.
    conn = _FakeConn(total=0, page_rows=[])
    dedup.list_pair_audit(
        conn, category_main="dum",
        districts=[DistrictChip(name="Jihlava", level="obec", id=586846)],
    )
    for s, _ in conn.executed:
        assert s.count("LEFT JOIN properties pl ON pl.id = a.left_property_id") == 1
        assert s.count("LEFT JOIN properties pr ON pr.id = a.right_property_id") == 1


def test_room_type_filters_on_detail_room_type() -> None:
    conn = _FakeConn(total=0, page_rows=[])
    dedup.list_pair_audit(conn, room_type="floor_plan")
    for s, params in conn.executed:
        assert "a.detail->>'room_type' = %(room_type)s" in s
        assert params["room_type"] == "floor_plan"


def test_floor_plan_factor_filters_on_reason() -> None:
    conn = _FakeConn(total=0, page_rows=[])
    dedup.list_pair_audit(conn, factor="floor_plan")
    for s, _ in conn.executed:
        assert "a.detail->>'reason' = 'floor_plan_different_layout'" in s


def test_property_id_in_batches_many_properties_with_any() -> None:
    conn = _FakeConn(total=1, page_rows=[_audit_row()])
    dedup.list_pair_audit(conn, property_id_in=[10, 20, 30])
    for s, params in conn.executed:
        assert "a.left_listing_id IN" in s
        assert "a.right_listing_id IN" in s
        assert "(SELECT id FROM listings WHERE property_id = ANY(%(audit_pids)s))" in s
        assert params["audit_pids"] == [10, 20, 30]


def test_property_id_in_empty_list_omits_the_clause() -> None:
    conn = _FakeConn(total=0, page_rows=[])
    dedup.list_pair_audit(conn, property_id_in=[])
    for s, params in conn.executed:
        assert "audit_pids" not in s
        assert "audit_pids" not in (params or {})


def test_no_category_main_or_districts_omits_the_properties_join() -> None:
    conn = _FakeConn(total=0, page_rows=[])
    dedup.list_pair_audit(conn, category_main=None, districts=None)
    for s, params in conn.executed:
        assert "LEFT JOIN properties pl" not in s
        assert "LEFT JOIN properties pr" not in s
        assert "category_main" not in (params or {})


def test_read_path_resolves_self_paired_rows_from_the_merge_ledger() -> None:
    # A legacy self-paired row (left_sreality_id == right_sreality_id, the display
    # bug) must be repaired at read time from the property_merge_events ledger, not
    # shown as one listing id twice. Guardrail: a refactor can't silently drop it.
    conn = _FakeConn(total=1, page_rows=[_audit_row()])
    dedup.list_pair_audit(conn)
    page_sql = next(s for s, _ in conn.executed if "ORDER BY a.run_at DESC" in s)
    assert "CASE WHEN a.left_sreality_id = a.right_sreality_id" in page_sql
    assert "property_merge_events" in page_sql


def test_ledger_side_sql_guards_against_a_null_in_the_moved_array() -> None:
    # property_merge_events.listing_id is legacy SREALITY-valued and NULL for a
    # moved post-Gate-2 non-sreality listing, so array_agg(listing_id) can itself
    # contain a NULL. `col = ANY(array-with-NULL)` is NULL (not false), which would
    # NULL out `NOT (...)` for every row and starve the survivor-side subquery to
    # empty. The generated SQL must filter that NULL out AND coalesce the
    # (possibly all-NULL) array before the `= ANY(...)` comparison.
    conn = _FakeConn(total=1, page_rows=[_audit_row()])
    dedup.list_pair_audit(conn)
    page_sql = next(s for s, _ in conn.executed if "ORDER BY a.run_at DESC" in s)
    assert "array_agg(listing_id) FILTER (WHERE listing_id IS NOT NULL)" in page_sql
    assert "ANY(COALESCE(_ev.moved, ARRAY[]::bigint[]))" in page_sql
    assert "survivor_property_id" in page_sql
    assert "array_agg(listing_id)" in page_sql


class _RecCur:
    """Cursor for _record_operator_decision: canned rows keyed by SQL shape."""

    def __init__(self, conn: "_RecConn") -> None:
        self._c = conn
        self._rows: list[tuple[Any, ...]] = []
        self._props = conn.props

    def __enter__(self) -> "_RecCur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._c.executed.append((s, params))
        if "FROM properties WHERE id IN" in s:
            # (id, repr_listing_id [legacy sreality-valued], repr_listing_ref_id
            #  [surrogate], category_main).
            self._rows = list(self._props)
        elif "array_agg(listing_id)" in s:
            # Ledger resolves the two sides to DISTINCT (sreality-valued) listings.
            self._rows = [(100, 200)]
        else:
            self._rows = []

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _RecConn:
    def __init__(self, props: list[tuple[Any, ...]] | None = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        # Default: both properties share the SAME repr (sreality 555, surrogate 5550)
        # — the post-merge drift that produced the self-paired bug.
        self.props = props if props is not None else [
            (10, 555, 5550, "byt"), (11, 555, 5550, "byt"),
        ]

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
    # The ledger query ran, and the INSERT drove BOTH the sreality columns and the
    # surrogate resolution off its DISTINCT ids (100, 200) — NOT the equal repr the
    # properties table returned (which would collapse both sides to one listing).
    assert any("array_agg(listing_id)" in s for s, _ in conn.executed)
    ins = _insert_params(conn)
    # Param order: (left_sreality_id, subquery_arg, left_ref_fallback,
    #               right_sreality_id, subquery_arg, right_ref_fallback, ...).
    # The surrogate column is COALESCE((SELECT id WHERE sreality_id=<disambiguated>),
    # repr_ref) — so the DISTINCT ledger sreality id drives the resolution, and the
    # (collapsed) repr_ref is only the NULL-sreality fallback.
    assert "COALESCE((SELECT id FROM listings WHERE sreality_id = %s)" in \
        next(s for s, _ in conn.executed if "INSERT INTO dedup_pair_audit" in s)
    assert ins[0] == 100 and ins[3] == 200        # sreality columns, disambiguated
    assert ins[1] == 100 and ins[4] == 200        # surrogate resolution args, distinct


def test_operator_dismiss_records_surrogate_from_repr_ref_and_skips_the_ledger() -> None:
    # A dismissal is not a merge (no group, no re-point) so the repr is correct and the
    # ledger must not be consulted; left/right_listing_id come from repr_listing_ref_id.
    conn = _RecConn(props=[(10, 700, 7000, "byt"), (11, 800, 8000, "byt")])
    dedup._record_operator_decision(
        conn, left_property_id=10, right_property_id=11,
        outcome="dismissed", markers={"stage": "visual"}, merge_group_id=None,
    )
    assert not any("array_agg(listing_id)" in s for s, _ in conn.executed)
    ins = _insert_params(conn)
    # sreality columns = repr_listing_id (700/800); surrogate fallback = repr_ref
    # (7000/8000) — the value used when the sreality lookup returns nothing.
    assert ins[0] == 700 and ins[3] == 800
    assert ins[2] == 7000 and ins[5] == 8000


def test_operator_decision_gate2_null_sreality_still_records_a_surrogate() -> None:
    # Post-Gate-2 a non-sreality property has repr_listing_id (sreality) NULL but a
    # non-NULL repr_listing_ref_id. The audit row must NOT land both *_listing_id NULL
    # (permanently unattributable) — it falls back to the surrogate repr_ref.
    conn = _RecConn(props=[(10, None, 480001, "byt"), (11, None, 480002, "byt")])
    dedup._record_operator_decision(
        conn, left_property_id=10, right_property_id=11,
        outcome="dismissed", markers={"stage": "visual"}, merge_group_id=None,
    )
    ins = _insert_params(conn)
    assert ins[0] is None and ins[3] is None      # legacy sreality columns are NULL
    assert ins[2] == 480001 and ins[5] == 480002  # surrogate fallback keeps it attributable
