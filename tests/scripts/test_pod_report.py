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


def _parse(line: str) -> list[dict]:
    assert line.startswith(pod_report.LINE_PREFIX)
    return json.loads(line[len(pod_report.LINE_PREFIX):])


def test_the_line_is_one_line_whatever_the_tail_contains():
    # The lane's UPDATE strips the previous heartbeat with a line-anchored regexp, so a
    # tail carrying newlines, quotes or backticks must not become a second line.
    tail = "error: could not build wheel\n  \"quoted\"\n  `backtick` 100%\n"
    line = pod_report.build_line([pod_report.build_record("exit=1 step=torch", tail=tail)])
    assert "\n" not in line
    (record,) = _parse(line)
    assert record["msg"] == "exit=1 step=torch"
    assert record["tail"] == tail
    assert record["ts"].startswith("20") and record["python"] and record["pod"]


# --- the bounded history (2026-09-08 (h)) -------------------------------------------


def test_the_history_is_bounded_and_keeps_the_newest_records():
    records = [pod_report.build_record(f"pass=1 step=s{i} ok") for i in range(20)]
    kept = _parse(pod_report.build_line(records))
    assert len(kept) == pod_report.HISTORY_LIMIT
    assert kept[-1]["msg"] == "pass=1 step=s19 ok"


def test_the_first_exit_report_is_never_the_one_dropped():
    # THE bug: a crash loop's later passes pushed the report naming the cause out of the
    # note, so the only line that mattered was the only line missing.
    records = [pod_report.build_record("pass=1 step=venv ok"),
               pod_report.build_record("pass=1 exit=1 step=torch",
                                       tail="No space left on device")]
    for pas in range(2, 8):
        records.append(pod_report.build_record(f"pass={pas} step=deps ok"))
        records.append(pod_report.build_record(f"pass={pas} exit=3 step=fetch"))
    kept = _parse(pod_report.build_line(records))
    assert len(kept) == pod_report.HISTORY_LIMIT
    assert kept[0]["msg"] == "pass=1 exit=1 step=torch"
    assert kept[0]["tail"] == "No space left on device"
    assert kept[-1]["msg"] == "pass=7 exit=3 step=fetch"


def test_the_line_stays_small_enough_to_live_in_a_note():
    # Eight 3000-char tails would be 24 kB; the note has to hold the lane's own text too.
    records = [pod_report.build_record(f"pass={i} exit=1 step=torch", tail="E" * 3000)
               for i in range(1, 9)]
    line = pod_report.build_line(records)
    assert len(line) <= pod_report.MAX_LINE_CHARS
    kept = _parse(line)          # still valid JSON, never a truncated string
    assert len(kept) == pod_report.HISTORY_LIMIT
    # The cause keeps its whole tail; the oldest symptoms are what get surrendered.
    assert kept[0]["tail"] == "E" * 3000
    assert not kept[1].get("tail")
    assert sum(1 for r in kept if r.get("tail")) < pod_report.HISTORY_LIMIT


def test_the_history_round_trips_through_the_file_that_survives_a_restart(tmp_path):
    path = tmp_path / "steps.json"
    assert pod_report.load_history(str(path)) == []
    pod_report.save_history(str(path), [pod_report.build_record("pass=1 step=uv ok")])
    (loaded,) = pod_report.load_history(str(path))
    assert loaded["msg"] == "pass=1 step=uv ok"
    # A half-written file costs the history, never the heartbeat.
    path.write_text("{not json", encoding="utf-8")
    assert pod_report.load_history(str(path)) == []
    assert pod_report.load_history("") == []


def test_main_appends_to_the_history_it_finds(monkeypatch, capsys, tmp_path):
    path = tmp_path / "steps.json"
    monkeypatch.setenv("PODBOOT_HISTORY", str(path))
    for key in ("HEARTBEAT_SQL", "HEARTBEAT_RUN_ID", "SUPABASE_DB_URL"):
        monkeypatch.delenv(key, raising=False)
    assert pod_report.main(["pass=1", "step=uv", "ok"]) == 0
    assert pod_report.main(["pass=2", "step=deps", "ok"]) == 0
    capsys.readouterr()
    assert [r["msg"] for r in pod_report.load_history(str(path))] == [
        "pass=1 step=uv ok", "pass=2 step=deps ok"]


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
    assert params["note"].startswith("pod steps [")


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
