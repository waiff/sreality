"""Lane entry point: arg parsing, the mode registry, and the always-written summary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autodedup import iterations, lane


def test_modes_registry_contains_census() -> None:
    assert "census" in lane.MODES
    assert callable(lane.MODES["census"])


def test_parse_kv_args_happy_path() -> None:
    assert lane.parse_kv_args("min_n=800, top=60") == {"min_n": "800", "top": "60"}
    assert lane.parse_kv_args("") == {}
    assert lane.parse_kv_args(None) == {}
    assert lane.parse_kv_args("a=1,,b=2") == {"a": "1", "b": "2"}
    assert lane.parse_kv_args("k=x:y-z.1") == {"k": "x:y-z.1"}
    assert lane.parse_kv_args("empty=") == {"empty": ""}


@pytest.mark.parametrize("raw", ["min_n", "=5", "a=1,a=2", "a=1,bare"])
def test_parse_kv_args_rejects_malformed(raw: str) -> None:
    with pytest.raises(ValueError):
        lane.parse_kv_args(raw)


def test_unknown_mode_exits_two_and_lists_the_known_modes(tmp_path: Path) -> None:
    code = lane.run("nope", "", tmp_path, conn_factory=lambda: None)
    assert code == 2
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["ok"] is False
    assert "census" in summary["error"]


def test_summary_is_written_when_the_connection_fails(tmp_path: Path) -> None:
    def boom() -> Any:
        raise RuntimeError("no database here")

    code = lane.run("census", "top=1", tmp_path, conn_factory=boom)
    assert code == 1
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["ok"] is False
    assert summary["mode"] == "census"
    assert summary["args"] == {"top": "1"}
    assert "no database here" in summary["error"]
    assert summary["started_at"] and summary["finished_at"]
    assert "traceback" in summary


def test_summary_is_written_when_args_are_malformed(tmp_path: Path) -> None:
    code = lane.run("census", "bare", tmp_path, conn_factory=lambda: None)
    assert code == 1
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["ok"] is False
    assert "malformed" in summary["error"]


def test_successful_mode_records_its_result(tmp_path: Path) -> None:
    lane.MODES["_probe"] = lambda cf, args, out: {"seen": args}
    try:
        code = lane.run("_probe", "a=1", tmp_path, conn_factory=lambda: None)
    finally:
        del lane.MODES["_probe"]
    assert code == 0
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["ok"] is True
    assert summary["result"] == {"seen": {"a": "1"}}
    assert summary["error"] is None


def test_main_parses_the_cli(tmp_path: Path) -> None:
    code = lane.main(["--mode", "nope", "--args", "x=1", "--out", str(tmp_path)])
    assert code == 2
    assert (tmp_path / "summary.json").exists()


def test_run_census_against_a_fake_connection(tmp_path: Path) -> None:
    from autodedup import census

    block_row = {
        "block_code": 1, "block_name": "Blok", "n_total": 3000, "n_active": 1000,
        "n_inactive": 2000, "n_served": 3000, "n_sources": 9, "cat_byt": 900,
        "cat_dum": 900, "cat_pozemek": 600, "cat_komercni": 600, "cat_ostatni": 0,
        "type_prodej": 2000, "type_pronajem": 1000, "type_drazba": 0, "type_podil": 0,
        "with_disposition": 900, "with_area": 2900, "with_floor": 800, "with_broker": 2000,
        "with_street": 2000, "with_point": 1500, "raw_inblock_pairs": 999,
    }
    detail_row = {
        "n_block_listings": 3000, "n_images": 30000, "n_listings_with_images": 2700,
        "n_listings_with_phash": 2600, "n_listings_with_clip": 2500,
        "shared_pin_listings": 100, "n_distinct_brokers": 40,
        "first_seen_min": None, "first_seen_max": None, "n_inactive_by_year": {"2025": 10},
    }

    class FakeCursor:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        def execute(self, sql: str, params: Any = None) -> None:
            if "statement_timeout" in sql:
                self.rows = []
            elif sql is census.CENSUS_DETAIL_SQL:
                self.rows = [detail_row]
            else:
                self.rows = [dict(block_row)]

        @property
        def description(self):
            return [(k,) for k in (self.rows[0] if self.rows else {})]

        def fetchall(self):
            return [tuple(r.values()) for r in self.rows]

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

    class FakeConn:
        def __init__(self) -> None:
            self.closed = False

        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def transaction(self):
            return _Noop()

        def close(self) -> None:
            self.closed = True

    class _Noop:
        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

    code = lane.run("census", "top=1,detail_top=1", tmp_path, conn_factory=FakeConn)
    assert code == 0, (tmp_path / "summary.json").read_text()
    report = json.loads((tmp_path / "census.json").read_text())
    assert report["ranking"][0]["block_code"] == 1
    assert report["parameters"]["top"] == 1
    assert set(report["grains"]) == set(census.GRAINS)


class _Noop:
    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _fake_conn(execute):
    class Cur:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        def execute(self, sql: str, params: Any = None) -> None:
            self.rows = [] if "statement_timeout" in sql else execute(sql)

        @property
        def description(self):
            return [(k,) for k in (self.rows[0] if self.rows else {})]

        def fetchall(self):
            return [tuple(r.values()) for r in self.rows]

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

    class Conn:
        def cursor(self) -> Cur:
            return Cur()

        def transaction(self):
            return _Noop()

        def close(self) -> None:
            return None

    return Conn


def test_detail_failure_is_recorded_and_the_block_census_survives(tmp_path: Path) -> None:
    from autodedup import census

    block_row = {"block_code": 7, "block_name": "Blok", "n_total": 3000, "n_sources": 9}

    def execute(sql: str) -> list[dict[str, Any]]:
        if sql is census.CENSUS_DETAIL_SQL:
            raise RuntimeError("canceling statement due to statement timeout")
        if sql is census.CENSUS_COVERAGE_SQL:
            return [{"n_listings": 10, "n_with_location_row": 9}]
        return [dict(block_row)]

    code = lane.run("census", "top=1,detail_top=1", tmp_path, conn_factory=_fake_conn(execute))
    assert code == 0
    report = json.loads((tmp_path / "census.json").read_text())
    assert report["coverage"]["n_listings"] == 10
    town = report["grains"]["town"][0]
    assert town["has_detail"] is False
    assert "statement timeout" in town["detail_error"]
    assert report["ranking"] == []
    assert len(report["errors"]) == 2


def test_block_layer_is_on_disk_before_the_detail_pass(tmp_path: Path) -> None:
    from autodedup import census

    seen: list[dict[str, Any]] = []

    def execute(sql: str) -> list[dict[str, Any]]:
        if sql is census.CENSUS_DETAIL_SQL:
            seen.append(json.loads((tmp_path / "census.json").read_text()))
            raise RuntimeError("stop here")
        if sql is census.CENSUS_COVERAGE_SQL:
            return [{"n_listings": 1}]
        return [{"block_code": 1, "block_name": "B", "n_total": 3000, "n_sources": 9}]

    lane.run("census", "top=1,detail_top=1", tmp_path, conn_factory=_fake_conn(execute))
    assert seen and seen[0]["grains"]["town"] and seen[0]["grains"]["quarter"]


def test_scrub_redacts_credentials_and_dsn_shapes() -> None:
    env = {"SUPABASE_DB_URL": "postgresql://u:hunter2@host/db", "R2_SECRET_ACCESS_KEY": "s" * 40}
    text = "invalid dsn: postgresql://u:hunter2@host/db and key sssssssssssssssssssssssssssssssssssssss s"
    out = lane.scrub(text, env=env)
    assert "hunter2" not in out
    assert "postgresql://" not in out
    assert lane.scrub("key " + "s" * 40, env=env) == "key ***"
    assert lane.scrub("nothing secret", env={}) == "nothing secret"


def test_scrub_ignores_short_or_missing_values() -> None:
    assert lane.scrub("abc", env={"QWEN_API_KEY": "abc"}) == "abc"
    assert lane.scrub("abc", env={"QWEN_API_KEY": ""}) == "abc"


# --- the record mode, from the lane's side ------------------------------------------------


def _ledger_conn(seen: list[str], *, row: Any = (7,)) -> Any:
    """A connection that answers the presence probe true and hands `row` back from every
    ledger statement. `seen` collects the statements the lane really executed."""

    class Cur:
        def __init__(self) -> None:
            self.row: Any = None

        def execute(self, sql: str, params: Any = None) -> None:
            if "statement_timeout" in sql:
                return
            seen.append(sql)
            self.row = (True,) if sql is iterations.ITERATIONS_PRESENT_SQL else row

        def fetchone(self) -> Any:
            return self.row

        def __enter__(self) -> "Cur":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    class Conn:
        def cursor(self) -> Cur:
            return Cur()

        def transaction(self):
            return _Noop()

        def close(self) -> None:
            return None

    return Conn()


def test_record_without_an_id_still_inserts(tmp_path: Path) -> None:
    seen: list[str] = []
    code = lane.run(
        "record",
        "wave=W0,title=Region census,tools=psql/GitHub Actions,cost_usd=0",
        tmp_path,
        conn_factory=lambda: _ledger_conn(seen),
    )
    assert code == 0
    assert iterations.RECORD_ITERATION_SQL in seen
    assert iterations.UPDATE_ITERATION_SQL not in seen
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["result"]["recorded"] is True
    assert summary["result"]["tools"] == ["psql", "GitHub Actions"]


def test_record_with_an_id_updates_and_files_no_new_row(tmp_path: Path) -> None:
    """An `id=` run is a CORRECTION — it must not insert anything, neither the corrected
    row again nor a wrapper row for the correction itself."""
    seen: list[str] = []
    code = lane.run(
        "record",
        "id=12,status=done,notes=billed after the pod was reaped",
        tmp_path,
        conn_factory=lambda: _ledger_conn(seen),
    )
    assert code == 0
    assert seen.count(iterations.UPDATE_ITERATION_SQL) == 1
    assert iterations.RECORD_ITERATION_SQL not in seen
    assert iterations.START_ITERATION_SQL not in seen
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["result"]["updated"] is True
    assert summary["result"]["updates"] == {
        "status": "done", "notes": "billed after the pod was reaped"
    }


def test_record_with_an_unknown_id_exits_two(tmp_path: Path) -> None:
    seen: list[str] = []
    code = lane.run(
        "record", "id=9999,cost_usd=1", tmp_path,
        conn_factory=lambda: _ledger_conn(seen, row=None),
    )
    assert code == 2
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["ok"] is False
    assert "9999" in summary["error"] and "no autodedup.iterations row" in summary["error"]


def test_record_with_an_id_refuses_to_change_the_wave(tmp_path: Path) -> None:
    seen: list[str] = []
    code = lane.run(
        "record", "id=12,wave=W9", tmp_path, conn_factory=lambda: _ledger_conn(seen)
    )
    assert code == 1
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert "wave" in summary["error"]
    assert seen == []


def test_a_failed_mode_keeps_the_summary_it_wrote_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The judge lane writes its artifact BEFORE raising, because a rented GPU that never
    served is the one failure whose price is only in that file. The wrapper's own summary must
    carry it, not overwrite it with an exception string."""
    def boom(factory: Any, args: dict[str, str], out_dir: Path) -> dict[str, Any]:
        lane.write_json(Path(out_dir) / "summary.json", {
            "pod_boot_failed": "PodBootstrapError: never served",
            "pod_boot_s": 1510.0,
            "gpu": "NVIDIA L40S",
            "usd_per_hr": 1.09,
        })
        raise RuntimeError("never served")

    monkeypatch.setitem(lane.MODES, "judge", boom)
    code = lane.run("judge", "", tmp_path, conn_factory=lambda: _ledger_conn([]))
    assert code == 1
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["ok"] is False and summary["result"] is None
    assert summary["partial_result"]["gpu"] == "NVIDIA L40S"
    assert summary["partial_result"]["pod_boot_s"] == 1510.0


def test_a_clean_run_carries_no_partial_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(lane.MODES, "judge", lambda factory, args, out_dir: {"done": 1})
    code = lane.run("judge", "", tmp_path, conn_factory=lambda: _ledger_conn([]))
    assert code == 0
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert "partial_result" not in summary and summary["result"] == {"done": 1}
