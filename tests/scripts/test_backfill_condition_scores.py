"""Hermetic tests for the pending-listing selection in backfill_condition_scores.

No DB, no network — a scripted cursor returns prepared rows in order, so
the tests assert SQL composition (kraj scope, sibling-reuse exclusion)
without connecting anywhere. psycopg imports live inside main(), so
importing the helpers is clean.
"""

from __future__ import annotations

from typing import Any

from scripts.backfill_condition_scores import (
    _ENABLED_REGIONS_KEY,
    _enabled_region_ids,
    _select_pending,
)


# ---- _enabled_region_ids ----------------------------------------------------


def test_enabled_region_ids_reads_the_settings_key():
    conn = _make_conn([("fetchone", ([27, 43, 86, 94, 108],))])
    assert _enabled_region_ids(conn) == [27, 43, 86, 94, 108]
    sql, params = conn.cursor_obj.executed[0]
    assert "FROM app_settings" in sql
    assert params == (_ENABLED_REGIONS_KEY,)


def test_enabled_region_ids_missing_row_returns_empty():
    conn = _make_conn([("fetchone", None)])
    assert _enabled_region_ids(conn) == []


def test_enabled_region_ids_empty_array_returns_empty():
    conn = _make_conn([("fetchone", ([],))])
    assert _enabled_region_ids(conn) == []


# ---- _select_pending: region scoping ----------------------------------------


def test_paused_when_no_override_and_no_enabled_regions():
    # Settings row missing -> effective list empty -> no listing query at all.
    conn = _make_conn([("fetchone", None)])
    out = _select_pending(conn, region_ids=[], max_age_days=30, limit=10)
    assert out == []
    assert len(conn.cursor_obj.executed) == 1, "must not run the pending query"


def test_settings_list_used_when_no_override():
    conn = _make_conn([
        ("fetchone", ([27, 43],)),       # app_settings lookup
        ("fetchall", [(101,), (202,)]),  # pending query
    ])
    out = _select_pending(conn, region_ids=[], max_age_days=30, limit=10)
    assert out == [101, 202]
    _, params = conn.cursor_obj.executed[-1]
    assert params == ("30 days", [27, 43], 10)


def test_explicit_override_wins_over_settings():
    conn = _make_conn([("fetchall", [(5,)])])
    out = _select_pending(conn, region_ids=[27], max_age_days=30, limit=10)
    assert out == [5]
    executed = conn.cursor_obj.executed
    assert len(executed) == 1, "override must skip the app_settings lookup"
    assert "app_settings" not in executed[0][0]
    assert executed[0][1] == ("30 days", [27], 10)


def test_region_clause_uses_admin_region_id_without_null_passthrough():
    conn = _make_conn([("fetchall", [])])
    _select_pending(conn, region_ids=[27], max_age_days=30, limit=10)
    sql = conn.cursor_obj.executed[0][0]
    assert "l.region_id = ANY(%s::bigint[])" in sql
    assert "locality_region_id" not in sql
    assert "l.region_id IS NULL" not in sql, "region_id NULL = parked, not selected"


def test_no_freshness_clause_when_max_age_days_zero():
    conn = _make_conn([("fetchall", [])])
    _select_pending(conn, region_ids=[27], max_age_days=0, limit=10)
    sql, params = conn.cursor_obj.executed[0]
    assert "::interval" not in sql
    assert params == ([27], 10)


# ---- _select_pending: category scope -----------------------------------------


def test_category_scope_limits_scoring_to_byt_and_dum():
    # The two-axis rubric (building + apartment) has no semantics for land
    # (pozemek) or ostatni — both walked since the category-parity expansion —
    # so the selector must never queue them for the LLM. komercni and
    # category_main NULL are parked by the same clause.
    conn = _make_conn([("fetchall", [])])
    _select_pending(conn, region_ids=[27], max_age_days=30, limit=10)
    sql = conn.cursor_obj.executed[0][0]
    assert "l.category_main IN ('byt', 'dum')" in sql


# ---- _select_pending: sibling-reuse exclusion --------------------------------


def test_sibling_reuse_exclusion_present():
    # A same-property sibling holding a GENUINE score parks this listing;
    # propagate_condition_levels copies the score instead of re-billing.
    conn = _make_conn([("fetchall", [])])
    _select_pending(conn, region_ids=[27], max_age_days=30, limit=10)
    sql = conn.cursor_obj.executed[0][0]
    assert "NOT EXISTS" in sql
    assert "sib.property_id = l.property_id" in sql
    assert "sib.sreality_id <> l.sreality_id" in sql
    assert "sib.condition_levels_propagated_from IS NULL" in sql
    assert "l.property_id IS NULL OR NOT EXISTS" in sql


def test_latest_snapshot_cache_predicate_kept():
    # Own-snapshot invalidation must still re-score the SCORED listing.
    conn = _make_conn([("fetchall", [])])
    _select_pending(conn, region_ids=[27], max_age_days=30, limit=10)
    sql = conn.cursor_obj.executed[0][0]
    assert "latest_snapshot" in sql
    assert "cs.id IS NULL" in sql


# ---- Helpers -----------------------------------------------------------------


class _ScriptedCursor:
    def __init__(self, plan: list[tuple[str, Any]]) -> None:
        self._plan = plan
        self._idx = 0
        self.executed: list[tuple[str, Any]] = []
        self._next: tuple[str, Any] | None = None

    def execute(self, sql: str, params: Any = None) -> None:
        if self._idx >= len(self._plan):
            raise AssertionError(f"execute past plan end (sql={sql[:80]!r})")
        self.executed.append((sql, params))
        self._next = self._plan[self._idx]

    def fetchone(self) -> Any:
        assert self._next is not None and self._next[0] == "fetchone"
        out = self._next[1]
        self._idx += 1
        self._next = None
        return out

    def fetchall(self) -> list[Any]:
        assert self._next is not None and self._next[0] == "fetchall"
        out = self._next[1] or []
        self._idx += 1
        self._next = None
        return out

    def __enter__(self) -> "_ScriptedCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _ScriptedConn:
    def __init__(self, plan: list[tuple[str, Any]]) -> None:
        self.cursor_obj = _ScriptedCursor(plan)

    def cursor(self) -> _ScriptedCursor:
        return self.cursor_obj


def _make_conn(plan: list[tuple[str, Any]]) -> _ScriptedConn:
    return _ScriptedConn(plan)
