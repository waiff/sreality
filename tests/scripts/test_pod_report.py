"""scripts/pod_report.py — the step heartbeat a pod writes DURING its bootstrap.

It runs where nothing else in this repo does: the image's Python 3.10, before the
checkout exists, with the database as the only channel out (RunPod's Pod logs endpoint
answers 400). The two properties that matter are that it says enough to diagnose the
failure, and that it can never BE the failure.
"""

from __future__ import annotations

import json
import sys
import types

from scripts import pod_report


def test_the_line_is_one_line_whatever_the_tail_contains():
    # The lane's UPDATE strips the previous heartbeat with a line-anchored regexp, so a
    # tail carrying newlines, quotes or backticks must not become a second line.
    tail = "error: could not build wheel\n  \"quoted\"\n  `backtick` 100%\n"
    line = pod_report.build_line("exit=1 step=torch", tail=tail)
    assert "\n" not in line
    assert line.startswith("pod step ")
    record = json.loads(line[len("pod step "):])
    assert record["msg"] == "exit=1 step=torch"
    assert record["tail"] == tail
    assert record["ts"].startswith("20") and record["python"] and record["pod"]


def test_the_tail_is_read_from_the_file_and_bounded(tmp_path):
    log = tmp_path / "bootstrap.log"
    log.write_text("x" * 10_000 + "THE ACTUAL ERROR\n", encoding="utf-8")
    tail = pod_report._tail(str(log))
    assert "THE ACTUAL ERROR" in tail
    assert len(tail) <= pod_report.TAIL_CHARS


def test_a_missing_log_is_reported_not_raised():
    assert "unreadable" in pod_report._tail("/nonexistent/bootstrap.log")


def test_without_the_wiring_it_prints_and_exits_zero(monkeypatch, capsys):
    # A lane that has not wired a heartbeat row (the production DINOv3 backfill today)
    # loses the heartbeat, never the bootstrap.
    for key in ("HEARTBEAT_SQL", "HEARTBEAT_RUN_ID", "SUPABASE_DB_URL"):
        monkeypatch.delenv(key, raising=False)
    assert pod_report.main(["step=torch ok"]) == 0
    assert "not reported" in capsys.readouterr().out


def test_it_executes_exactly_the_sql_the_lane_supplied(monkeypatch):
    executed: list[tuple[str, dict]] = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params):
            executed.append((sql, params))

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cur()

    fake = types.ModuleType("psycopg")
    fake.connect = lambda *a, **k: _Conn()  # noqa: ARG005
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    monkeypatch.setenv("HEARTBEAT_SQL", "UPDATE t SET note = %(note)s WHERE id = %(run_id)s")
    monkeypatch.setenv("HEARTBEAT_RUN_ID", "7")
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://fake")

    assert pod_report.main(["step=fetch ok"]) == 0
    (sql, params), = executed
    assert sql.startswith("UPDATE t SET note")
    # The row id is an int when it looks like one — a lane keyed on a text id still works.
    assert params["run_id"] == 7
    assert params["note"].startswith("pod step ")


def test_a_database_that_refuses_the_write_never_fails_the_bootstrap(monkeypatch, capsys):
    fake = types.ModuleType("psycopg")

    def _boom(*a, **k):  # noqa: ARG001
        raise RuntimeError("connection refused")

    fake.connect = _boom
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    monkeypatch.setenv("HEARTBEAT_SQL", "UPDATE t SET note = %(note)s WHERE id = %(run_id)s")
    monkeypatch.setenv("HEARTBEAT_RUN_ID", "7")
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://fake")

    assert pod_report.main(["step=fetch ok"]) == 0
    assert "report FAILED (ignored)" in capsys.readouterr().out
