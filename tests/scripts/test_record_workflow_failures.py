"""Hermetic tests for select_failed_runs() in record_workflow_failures.

No DB, no network — psycopg import lives inside main(), so importing the
filter is clean.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from scripts.record_workflow_failures import (
    MONITOR_WORKFLOW_NAME,
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
        _run(id=3, conclusion="cancelled"),
        _run(id=4, conclusion="timed_out"),
        _run(id=5, conclusion="startup_failure"),
        _run(id=6, conclusion="skipped"),
        _run(id=7, conclusion=None),
    ]
    kept = select_failed_runs(runs, since=SINCE)
    assert [r["run_id"] for r in kept] == [2, 4, 5]


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
