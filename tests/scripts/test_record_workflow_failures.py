"""Hermetic tests for select_failed_runs() in record_workflow_failures.

No DB, no network — psycopg import lives inside main(), so importing the
filter is clean.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from scripts.record_workflow_failures import (
    CANCELLED_MIN_DURATION_MINUTES,
    MONITOR_WORKFLOW_NAME,
    _read_cursor,
    _write_cursor,
    parse_ts,
    select_failed_runs,
    select_latest_successes,
)

SINCE = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)


def _run(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 101,
        "name": "Scraping: Sreality index walk",
        "path": ".github/workflows/index_walk.yml",
        "conclusion": "failure",
        "updated_at": "2026-06-11T12:30:00Z",
        "run_started_at": "2026-06-11T12:05:00Z",
        "html_url": "https://github.com/waiff/sreality/actions/runs/101",
    }
    base.update(overrides)
    return base


def test_keeps_only_alerting_conclusions():
    runs = [
        _run(id=1, conclusion="success"),
        _run(id=2, conclusion="failure"),
        # a QUICK cancel (1 min) — a supersession, not a failure → excluded
        _run(id=3, conclusion="cancelled", run_started_at="2026-06-11T12:29:00Z"),
        _run(id=4, conclusion="timed_out"),
        _run(id=5, conclusion="startup_failure"),
        _run(id=6, conclusion="skipped"),
        _run(id=7, conclusion=None),
    ]
    kept = select_failed_runs(runs, since=SINCE)
    assert [r["run_id"] for r in kept] == [2, 4, 5]


def test_cancelled_long_but_never_started_is_skipped():
    """A superseded run that sat QUEUED >= 8 min looks like a timeout kill by duration
    (run_started_at = queue entry), but its jobs never started — with started_check
    provided, it must be skipped; a genuinely started long cancel is kept. Without
    started_check (None), duration-only behavior is preserved."""
    long_cancel = _run(id=1, conclusion="cancelled",
                       run_started_at="2026-06-11T12:00:00Z",
                       updated_at="2026-06-11T12:20:00Z")
    # never started -> skipped
    assert select_failed_runs([long_cancel], since=SINCE,
                              started_check=lambda rid: False) == []
    # actually ran -> kept
    assert [r["run_id"] for r in select_failed_runs(
        [long_cancel], since=SINCE, started_check=lambda rid: True)] == [1]
    # no checker (pure mode) -> kept, as before
    assert [r["run_id"] for r in select_failed_runs([long_cancel], since=SINCE)] == [1]
    # the checker is NOT consulted for plain failures (would raise if called)
    def _boom(rid):
        raise AssertionError("started_check must not run for conclusion=failure")
    assert [r["run_id"] for r in select_failed_runs(
        [_run(id=2, conclusion="failure")], since=SINCE, started_check=_boom)] == [2]


def test_cancelled_kept_only_when_it_ran_long_enough():
    long_min = CANCELLED_MIN_DURATION_MINUTES + 5
    runs = [
        # timeout-minutes kill: ran to its budget → kept
        _run(id=1, conclusion="cancelled",
             run_started_at="2026-06-11T12:00:00Z",
             updated_at=f"2026-06-11T12:{long_min:02d}:00Z"),
        # cancel-in-progress supersession: killed in 2 min → dropped
        _run(id=2, conclusion="cancelled",
             run_started_at="2026-06-11T12:28:00Z", updated_at="2026-06-11T12:30:00Z"),
        # cancelled with no start time → can't judge → dropped (conservative)
        _run(id=3, conclusion="cancelled", run_started_at=None),
    ]
    assert [r["run_id"] for r in select_failed_runs(runs, since=SINCE)] == [1]


def test_excludes_the_monitor_itself():
    runs = [
        _run(id=1, name=MONITOR_WORKFLOW_NAME),
        _run(id=2, name="Monitoring: LLM pipeline liveness"),
    ]
    kept = select_failed_runs(runs, since=SINCE)
    assert [r["run_id"] for r in kept] == [2]


def test_drops_runs_completed_before_the_window():
    runs = [
        _run(id=1, updated_at="2026-06-11T11:59:59Z"),
        _run(id=2, updated_at="2026-06-11T12:00:00Z"),  # boundary: kept
        _run(id=3, updated_at=None),
        _run(id=4, updated_at="not-a-timestamp"),
    ]
    kept = select_failed_runs(runs, since=SINCE)
    assert [r["run_id"] for r in kept] == [2]


def test_row_shape_and_timestamp_parsing():
    (row,) = select_failed_runs([_run()], since=SINCE)
    assert row == {
        "run_id": 101,
        "workflow_name": "Scraping: Sreality index walk",
        "workflow_path": ".github/workflows/index_walk.yml",
        "conclusion": "failure",
        "run_started_at": datetime(2026, 6, 11, 12, 5, tzinfo=timezone.utc),
        "html_url": "https://github.com/waiff/sreality/actions/runs/101",
    }


def test_tolerates_missing_optional_fields():
    (row,) = select_failed_runs(
        [_run(name=None, path=None, run_started_at=None, html_url=None)], since=SINCE,
    )
    assert row["workflow_name"] == "(unnamed)"
    assert row["workflow_path"] is None
    assert row["run_started_at"] is None
    assert row["html_url"] is None


# --- select_latest_successes -----------------------------------------------


def test_latest_success_picks_newest_per_path():
    runs = [
        _run(id=1, conclusion="success", path="a.yml", run_started_at="2026-06-11T10:00:00Z"),
        _run(id=2, conclusion="success", path="a.yml", run_started_at="2026-06-11T12:00:00Z"),
        _run(id=3, conclusion="success", path="b.yml", run_started_at="2026-06-11T09:00:00Z"),
    ]
    by_path = {s["workflow_path"]: s for s in select_latest_successes(runs)}
    assert by_path["a.yml"]["last_success_run_id"] == 2  # newer wins
    assert by_path["a.yml"]["last_success_at"] == datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
    assert by_path["b.yml"]["last_success_run_id"] == 3


def test_latest_success_ignores_non_success_and_pathless():
    runs = [
        _run(id=1, conclusion="failure", path="a.yml"),
        _run(id=2, conclusion="cancelled", path="a.yml"),
        _run(id=3, conclusion="success", path=None),
    ]
    assert select_latest_successes(runs) == []


def test_parse_ts_handles_z_suffix_and_garbage():
    assert parse_ts("2026-06-11T12:00:00Z") == SINCE
    assert parse_ts(None) is None
    assert parse_ts("") is None
    assert parse_ts("garbage") is None


# --- high-water-mark cursor -------------------------------------------------


class _FakeCursor:
    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store
        self._row: Any = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        if "SELECT value" in sql:
            self._row = (self._store.get("val"),)
        elif "INSERT INTO app_settings" in sql:
            self._store["val"] = params[1]  # the ISO timestamp string

    def fetchone(self) -> Any:
        return self._row


class _FakeConn:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.store)


def test_cursor_read_write_roundtrip():
    conn = _FakeConn()
    assert _read_cursor(conn) is None  # unset on first ever run
    ts = datetime(2026, 7, 9, 21, 50, tzinfo=timezone.utc)
    _write_cursor(conn, ts)
    assert _read_cursor(conn) == ts
