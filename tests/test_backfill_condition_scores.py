"""Hermetic tests for scripts/backfill_condition_scores.py.

Covers the two non-trivial pieces:
  1. `_parse_region_ids` — CLI string → list[int] with sane error paths.
  2. `_select_pending` — SQL shape verification against a scripted
     cursor. The query has a few must-have invariants (LEFT JOIN on
     listing_condition_scores, cs.id IS NULL filter, region cardinality
     gate, ORDER BY last_seen_at DESC, LIMIT) that we want a guard
     against silent edits.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


def _load_backfill_module() -> Any:
    if "backfill_condition_scores" in sys.modules:
        return sys.modules["backfill_condition_scores"]
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "backfill_condition_scores.py"
    spec = importlib.util.spec_from_file_location(
        "backfill_condition_scores", path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["backfill_condition_scores"] = module
    spec.loader.exec_module(module)
    return module


# ---- _parse_region_ids ----------------------------------------------------


def test_parse_empty_returns_empty_list():
    m = _load_backfill_module()
    assert m._parse_region_ids("") == []
    assert m._parse_region_ids("   ") == []


def test_parse_single_id():
    m = _load_backfill_module()
    assert m._parse_region_ids("10") == [10]


def test_parse_four_kraje_default():
    """The workflow's default value."""
    m = _load_backfill_module()
    assert m._parse_region_ids("10,11,2,13") == [10, 11, 2, 13]


def test_parse_handles_whitespace_and_trailing_commas():
    m = _load_backfill_module()
    assert m._parse_region_ids(" 10 , 11 , 2 , 13 ,") == [10, 11, 2, 13]


def test_parse_rejects_non_integer_entry():
    m = _load_backfill_module()
    with pytest.raises(SystemExit) as exc_info:
        m._parse_region_ids("10,abc,13")
    assert exc_info.value.code == 2


# ---- _select_pending ------------------------------------------------------


class _ScriptedCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.executed: list[tuple[str, Any]] = []
        self._rows = rows

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def __enter__(self) -> "_ScriptedCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _ScriptedConn:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.cursor_obj = _ScriptedCursor(rows)

    def cursor(self) -> _ScriptedCursor:
        return self.cursor_obj


def test_select_pending_returns_int_list():
    m = _load_backfill_module()
    conn = _ScriptedConn([(100,), (200,), (300,)])
    out = m._select_pending(
        conn, region_ids=[10, 11, 2, 13], max_age_days=30, limit=500,
    )
    assert out == [100, 200, 300]
    assert all(isinstance(x, int) for x in out)


def test_select_pending_sql_invariants():
    """Pin the query shape so a casual edit can't drop the must-have
    invariants. If any of these assertions fails, ask why."""
    m = _load_backfill_module()
    conn = _ScriptedConn([])
    m._select_pending(
        conn, region_ids=[10, 11, 2, 13], max_age_days=30, limit=500,
    )
    sql, params = conn.cursor_obj.executed[0]
    # Latest-snapshot CTE
    assert "WITH latest_snapshot AS" in sql
    # LEFT JOIN to exclude already-scored listings
    assert "LEFT JOIN listing_condition_scores cs" in sql
    assert "cs.id IS NULL" in sql
    # is_active filter
    assert "l.is_active = true" in sql
    # Region cardinality gate (empty list = no filter)
    assert "cardinality(" in sql
    assert "l.locality_region_id = ANY" in sql
    # Non-sreality portals (region_id NULL) are never excluded by the
    # region filter — otherwise they'd never get condition-scored.
    assert "l.locality_region_id IS NULL" in sql
    # Freshness window
    assert "last_seen_at > now() - %s::interval" in sql
    # Newest first
    assert "ORDER BY l.last_seen_at DESC" in sql
    # Cap
    assert "LIMIT %s" in sql
    # Param order: interval, region_ids (×2), limit
    assert params == ("30 days", [10, 11, 2, 13], [10, 11, 2, 13], 500)


def test_select_pending_max_age_days_zero_drops_freshness_clause():
    """`max_age_days <= 0` is the operator's escape hatch to score older
    listings. The SQL must NOT include the `last_seen_at > now() - ...`
    branch, and the params tuple must drop the interval value."""
    m = _load_backfill_module()
    conn = _ScriptedConn([])
    m._select_pending(
        conn, region_ids=[10, 11], max_age_days=0, limit=500,
    )
    sql, params = conn.cursor_obj.executed[0]
    assert "l.is_active = true" in sql  # still applies
    assert "last_seen_at >" not in sql  # freshness gate gone
    # Params: region_ids (×2), limit — no interval prefix
    assert params == ([10, 11], [10, 11], 500)


def test_select_pending_negative_max_age_days_also_drops_freshness():
    m = _load_backfill_module()
    conn = _ScriptedConn([])
    m._select_pending(
        conn, region_ids=[], max_age_days=-1, limit=10,
    )
    sql, params = conn.cursor_obj.executed[0]
    assert "last_seen_at >" not in sql
    assert params == ([], [], 10)


def test_select_pending_with_empty_region_filter_still_passes_cardinality_gate():
    """Empty list path is the operator's 'do everything' override. The
    query passes [] to the cardinality(...) gate, which evaluates to 0
    so the region check short-circuits to true."""
    m = _load_backfill_module()
    conn = _ScriptedConn([])
    m._select_pending(
        conn, region_ids=[], max_age_days=30, limit=500,
    )
    _, params = conn.cursor_obj.executed[0]
    assert params[1] == []  # region_ids
    assert params[2] == []  # region_ids (second binding)
