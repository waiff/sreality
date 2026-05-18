"""Hermetic tests for the condition-scoring phase in scraper/main.py.

Two concerns:
  1. `_pending_condition_scores` produces the right SQL shape — no
     region filter (B4 keeps the scrape pipeline scoring everywhere),
     `cs.id IS NULL` exclusion of already-scored listings, freshness
     window, latest-snapshot CTE.
  2. `_run_condition_scoring` gates on ANTHROPIC_API_KEY and on
     `max_scores <= 0` without crashing or making DB connections.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from scraper import main as scraper_main


# ---- _pending_condition_scores -------------------------------------------


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


def test_pending_condition_scores_returns_int_list():
    conn = _ScriptedConn([(100,), (200,), (300,)])
    out = scraper_main._pending_condition_scores(conn, limit=10)
    assert out == [100, 200, 300]
    assert all(isinstance(x, int) for x in out)


def test_pending_condition_scores_sql_invariants():
    """Pin the SQL shape:
      - latest_snapshot CTE
      - LEFT JOIN listing_condition_scores cs ... cs.id IS NULL
      - is_active = true
      - 30-day freshness window
      - ORDER BY last_seen_at DESC
      - LIMIT %s
      - NO region filter (scrape pipeline scores everywhere)
    """
    conn = _ScriptedConn([])
    scraper_main._pending_condition_scores(conn, limit=200)
    sql, params = conn.cursor_obj.executed[0]
    assert "WITH latest_snapshot AS" in sql
    assert "LEFT JOIN listing_condition_scores cs" in sql
    assert "cs.id IS NULL" in sql
    assert "l.is_active = true" in sql
    assert "last_seen_at > now() - interval '30 days'" in sql
    assert "ORDER BY l.last_seen_at DESC" in sql
    assert "LIMIT %s" in sql
    # Scrape pipeline keeps things simple: no region filter.
    assert "locality_region_id" not in sql
    assert "cardinality(" not in sql
    assert params == (200,)


# ---- _run_condition_scoring gates ----------------------------------------


def test_run_condition_scoring_no_op_when_max_zero(caplog, monkeypatch):
    """`--max-condition-scores=0` is the explicit-disable path. The
    function must log a skip and return without touching the DB."""
    # If anything tried to connect to the DB this would explode.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-not-used")
    with caplog.at_level("INFO"):
        scraper_main._run_condition_scoring(max_scores=0)
    assert any("SCORE skipped" in m for m in caplog.messages)


def test_run_condition_scoring_no_op_when_api_key_missing(caplog, monkeypatch):
    """Mirrors `_run_image_downloads`'s gate on R2 env vars — a deploy
    without ANTHROPIC_API_KEY should not crash the scrape."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with caplog.at_level("INFO"):
        scraper_main._run_condition_scoring(max_scores=200)
    assert any("ANTHROPIC_API_KEY not set" in m for m in caplog.messages)
