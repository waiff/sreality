"""The progress ledger: what it writes, what it refuses to break, and how the lane wraps it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autodedup import iterations, lane


class _Noop:
    def __enter__(self) -> "_Noop":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _conn(*, present: bool = True, calls: list[tuple[str, dict[str, Any]]] | None = None) -> Any:
    seen = calls if calls is not None else []

    class Cur:
        def __init__(self) -> None:
            self.row: tuple[Any, ...] | None = None

        def execute(self, sql: str, params: Any = None) -> None:
            seen.append((sql, dict(params or {})))
            if sql is iterations.ITERATIONS_PRESENT_SQL:
                self.row = (present,)
            else:
                self.row = (7,)

        def fetchone(self) -> tuple[Any, ...] | None:
            return self.row

        def __enter__(self) -> "Cur":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    class Conn:
        calls = seen

        def cursor(self) -> Cur:
            return Cur()

        def transaction(self) -> _Noop:
            return _Noop()

        def close(self) -> None:
            return None

    return Conn()


def _params(calls: list[tuple[str, dict[str, Any]]], sql: str) -> dict[str, Any]:
    for seen_sql, params in calls:
        if seen_sql is sql:
            return params
    raise AssertionError("statement never executed")


# --- start / finish ----------------------------------------------------------------------


def test_start_iteration_writes_a_running_row() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    iteration_id = iterations.start_iteration(
        _conn(calls=calls),
        wave="W1",
        title="Cohort export",
        approach="dump the cohort",
        tools=["autodedup.export", "GitHub Actions"],
        run_id="123",
        artifacts={"actions_run": "https://example.test/1"},
    )
    assert iteration_id == 7
    params = _params(calls, iterations.START_ITERATION_SQL)
    assert params["wave"] == "W1" and params["title"] == "Cohort export"
    assert params["tools"] == ["autodedup.export", "GitHub Actions"]
    assert params["run_id"] == 123
    assert json.loads(params["artifacts"]) == {"actions_run": "https://example.test/1"}
    assert "'running'" in iterations.START_ITERATION_SQL


def test_finish_iteration_stamps_a_terminal_status() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    assert (
        iterations.finish_iteration(
            _conn(calls=calls),
            7,
            status="done",
            sample_stats={"counts": {"listings": 2}},
            metrics={"bytes": 10},
            cost_usd=0,
            notes=None,
        )
        == 7
    )
    params = _params(calls, iterations.FINISH_ITERATION_SQL)
    assert params["id"] == 7 and params["status"] == "done"
    assert json.loads(params["sample_stats"]) == {"counts": {"listings": 2}}
    assert json.loads(params["metrics"]) == {"bytes": 10}
    assert params["cost_usd"] == 0 and params["notes"] is None


def test_finish_iteration_is_a_noop_without_an_open_row() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    assert iterations.finish_iteration(_conn(calls=calls), None, status="done") is None
    assert iterations.finish_iteration(None, 7, status="done") is None
    assert calls == []


@pytest.mark.parametrize("fn", ["finish", "record"])
def test_an_unknown_status_is_refused(fn: str) -> None:
    conn = _conn()
    with pytest.raises(ValueError):
        if fn == "finish":
            iterations.finish_iteration(conn, 7, status="halfway")
        else:
            iterations.record_iteration(conn, wave="W1", title="x", status="halfway")


# --- missing schema ----------------------------------------------------------------------


def test_a_missing_schema_costs_the_row_not_the_run() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    conn = _conn(present=False, calls=calls)
    assert iterations.store_ready(conn) is False
    assert iterations.start_iteration(conn, wave="W1", title="x") is None
    assert iterations.record_iteration(conn, wave="W1", title="x") is None
    ledger_sql = [sql for sql, _ in calls if "statement_timeout" not in sql]
    assert ledger_sql == [iterations.ITERATIONS_PRESENT_SQL] * 3
    assert "to_regclass('autodedup.iterations')" in iterations.ITERATIONS_PRESENT_SQL


def test_every_ledger_statement_runs_under_a_statement_timeout() -> None:
    """A lock on autodedup.iterations must surface as the caught exception this module
    already handles, never as a wait that outlasts the lane."""
    calls: list[tuple[str, dict[str, Any]]] = []
    conn = _conn(calls=calls)
    iterations.start_iteration(conn, wave="W1", title="x")
    iterations.finish_iteration(conn, 7, status="done")
    guards = [sql for sql, _ in calls if "statement_timeout" in sql]
    assert guards and all(str(iterations.LEDGER_TIMEOUT_MS) in sql for sql in guards)
    # one guard per real statement: the presence probe and the insert, then the update
    assert len(guards) == len([sql for sql, _ in calls if "statement_timeout" not in sql])


def test_store_ready_swallows_a_broken_connection() -> None:
    class Boom:
        def cursor(self) -> Any:
            raise RuntimeError("connection is closed")

    assert iterations.store_ready(Boom()) is False
    assert iterations.store_ready(None) is False


def test_json_param_truncates_an_oversized_payload() -> None:
    assert iterations.json_param(None) is None
    payload = iterations.json_param({"blob": "x" * 50_000})
    assert payload is not None
    decoded = json.loads(payload)
    assert decoded["truncated"] is True and decoded["bytes"] > iterations.MAX_STATS_BYTES
    assert len(payload.encode("utf-8")) <= iterations.MAX_STATS_BYTES


# --- record mode -------------------------------------------------------------------------


def test_parse_record_args_splits_tools_on_semicolons() -> None:
    params = iterations.parse_record_args(
        {
            "wave": "W0",
            "title": "Region census",
            "tools": "autodedup.census; GitHub Actions ;psql",
            "cost_usd": "0.42",
            "run_id": "35088558149",
            "notes": "delta probes land in W0b",
        }
    )
    assert params["tools"] == ["autodedup.census", "GitHub Actions", "psql"]
    assert params["cost_usd"] == 0.42
    assert params["run_id"] == 35088558149
    assert params["status"] == "done"
    assert params["approach"] is None


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"wave": "W0"},
        {"title": "x"},
        {"wave": "W0", "title": "x", "status": "halfway"},
        {"wave": "W0", "title": "x", "cost_usd": "free"},
        {"wave": "W0", "title": "x", "nope": "1"},
    ],
)
def test_parse_record_args_rejects_nonsense(args: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        iterations.parse_record_args(args)


def test_record_mode_inserts_one_finished_row(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    conn = _conn(calls=calls)
    result = iterations.run_record(
        lambda: conn,
        {"wave": "W0", "title": "Region census", "tools": "psql", "run_id": "42"},
        tmp_path,
    )
    assert result["iteration_id"] == 7 and result["recorded"] is True
    params = _params(calls, iterations.RECORD_ITERATION_SQL)
    assert params["wave"] == "W0" and params["status"] == "done"
    assert params["tools"] == ["psql"] and params["run_id"] == 42
    assert json.loads(params["artifacts"]) == {
        "actions_run": f"{iterations.ACTIONS_RUN_URL}42"
    }
    assert "now()" in iterations.RECORD_ITERATION_SQL


def test_record_mode_without_the_schema_reports_it(tmp_path: Path) -> None:
    result = iterations.run_record(
        lambda: _conn(present=False), {"wave": "W0", "title": "x"}, tmp_path
    )
    assert result["iteration_id"] is None and result["recorded"] is False


# --- the lane wrapper --------------------------------------------------------------------


def test_every_mode_is_registered() -> None:
    assert set(lane.MODES) == {"census", "probes", "export", "judge", "score", "record"}
    # `record` writes its own terminal row, so wrapping it would file the same iteration twice.
    assert set(lane.ITERATION_META) == {"census", "probes", "export", "judge", "score"}
    for meta in lane.ITERATION_META.values():
        assert meta["wave"] and meta["title"] and meta["approach"] and meta["tools"]


def test_a_mode_run_opens_and_closes_its_ledger_row(tmp_path: Path, monkeypatch: Any) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    conn = _conn(calls=calls)
    monkeypatch.setitem(lane.MODES, "export", lambda f, a, o: {"counts": {"listings": 2}})
    monkeypatch.setenv("GITHUB_RUN_ID", "99")

    code = lane.run("export", "", tmp_path, conn_factory=lambda: conn)
    assert code == 0
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["iteration_id"] == 7 and "iteration_error" not in summary

    start = _params(calls, iterations.START_ITERATION_SQL)
    assert start["wave"] == "W1" and start["run_id"] == 99
    finish = _params(calls, iterations.FINISH_ITERATION_SQL)
    assert finish["status"] == "done"
    assert json.loads(finish["sample_stats"]) == {"counts": {"listings": 2}}
    assert json.loads(finish["metrics"]) == {"counts": {"listings": 2}}
    assert json.loads(finish["artifacts"]) == {"actions_run": f"{iterations.ACTIONS_RUN_URL}99"}


def test_a_failed_mode_leaves_a_failed_row(tmp_path: Path, monkeypatch: Any) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    conn = _conn(calls=calls)

    def boom(factory: Any, args: Any, out: Any) -> dict[str, Any]:
        raise RuntimeError("cohort walked off a cliff")

    monkeypatch.setitem(lane.MODES, "export", boom)
    assert lane.run("export", "", tmp_path, conn_factory=lambda: conn) == 1
    finish = _params(calls, iterations.FINISH_ITERATION_SQL)
    assert finish["status"] == "failed"
    assert "cliff" in finish["notes"]


def test_a_ledger_failure_never_fails_the_lane(tmp_path: Path, monkeypatch: Any) -> None:
    class Boom:
        def cursor(self) -> Any:
            raise RuntimeError("no ledger today")

        def close(self) -> None:
            return None

    monkeypatch.setitem(lane.MODES, "export", lambda f, a, o: {"ok": True})
    code = lane.run("export", "", tmp_path, conn_factory=lambda: Boom())
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert code == 0 and summary["ok"] is True
    assert summary["iteration_id"] is None


def test_a_ledger_open_that_raises_still_closes_its_connection(tmp_path: Path, monkeypatch: Any) -> None:
    """A dropped connection would hold a transaction-pooler slot for the whole mode run —
    up to five hours for an export, which itself needs that slot."""
    closed: list[bool] = []

    class Conn:
        def close(self) -> None:
            closed.append(True)

    def boom(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("ledger insert blew up")

    monkeypatch.setattr(iterations, "start_iteration", boom)
    monkeypatch.setitem(lane.MODES, "export", lambda f, a, o: {"ok": True})
    code = lane.run("export", "", tmp_path, conn_factory=lambda: Conn())
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert code == 0 and summary["ok"] is True
    assert "ledger insert blew up" in summary["iteration_error"]
    assert closed == [True]


def test_record_mode_is_not_double_filed(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    conn = _conn(calls=calls)
    code = lane.run("record", "wave=W0,title=Region census", tmp_path, conn_factory=lambda: conn)
    assert code == 0
    assert [sql for sql, _ in calls].count(iterations.RECORD_ITERATION_SQL) == 1
    assert iterations.START_ITERATION_SQL not in [sql for sql, _ in calls]
