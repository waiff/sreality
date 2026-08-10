"""Load orchestration: version bookkeeping, checkpointing, and the DB-free dry run.

The fake connection here proves control flow and SQL shape only — it cannot enforce a
CHECK, a UNIQUE or an FK, so nothing in this file claims the mirror accepted a row.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from location_data import load_assertions, loader_db, ruian_load


class _FakeCursor:
    def __init__(self, conn: "_FakeConn"):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(str(sql).split()), params))
        self.conn.result = self.conn.next_result(sql)

    def fetchone(self):
        return self.conn.result

    def fetchall(self):
        return self.conn.result if isinstance(self.conn.result, list) else []


class _FakeConn:
    """Scripted responses keyed by a substring of the SQL."""

    def __init__(self, script: list[tuple[str, object]] | None = None):
        self.script = script or []
        self.executed: list[tuple[str, object]] = []
        self.result: object = None

    def next_result(self, sql: str):
        flat = " ".join(str(sql).split())
        for key, value in self.script:
            if key in flat:
                return value
        return None

    def cursor(self):
        return _FakeCursor(self)

    def transaction(self):
        return _Noop()


class _Noop:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_version_label_is_the_load_event_not_the_product():
    assert ruian_load.version_label(datetime.date(2026, 7, 31)) == "ruian:2026-07-31"


def test_every_level_has_a_label_prefix_and_they_are_unique():
    assert set(ruian_load.LEVEL_ORDER) == set(ruian_load.LABEL_PREFIX)
    assert len(set(ruian_load.LABEL_PREFIX.values())) == len(ruian_load.LABEL_PREFIX)
    for prefix in ruian_load.LABEL_PREFIX.values():
        assert prefix.isalnum() and prefix.islower()  # ltree admits [A-Za-z0-9_] only


def test_parents_are_ordered_before_children():
    order = ruian_load.LEVEL_ORDER
    assert order.index("kraj") < order.index("okres") < order.index("obec")
    assert order.index("obec") < order.index("cast_obce")
    assert order.index("katastralni_uzemi") < order.index("zsj")


def _artifact(name: str) -> object:
    from location_data.ruian_csv import Artifact

    return Artifact(name=name, url=f"https://x/{name}.zip", path=Path("/tmp/x"),
                    bytes=10, sha256="abc", etag='"e"', last_modified="lm")


def test_ensure_version_inserts_one_version_for_both_products():
    conn = _FakeConn([("SELECT id, is_current FROM registry_versions", None),
                      ("INSERT INTO registry_versions", (77,))])
    artifacts = {"csv_ob_adr": _artifact("ob"), "csv_strukt_adr": _artifact("strukt")}
    version_id, current = ruian_load.ensure_version(
        conn, datetime.date(2026, 7, 31), artifacts,
        {"proj_version": "PROJ 9", "proj_pipeline": "S-JTSK to WGS 84 (5)"},
    )
    assert (version_id, current) == (77, False)
    insert = next(sql for sql, _ in conn.executed if sql.startswith("INSERT INTO registry_versions"))
    assert "is_current" in insert
    params = conn.executed[-1][1]
    assert params[0] == "ruian:2026-07-31"
    urls = json.loads(params[2])
    assert set(urls) == {"csv_ob_adr", "csv_strukt_adr"}


def test_ensure_version_resumes_an_unpublished_load():
    conn = _FakeConn([("SELECT id, is_current FROM registry_versions", (5, False))])
    version_id, current = ruian_load.ensure_version(
        conn, datetime.date(2026, 7, 31),
        {"csv_ob_adr": _artifact("ob")},
        {"proj_version": "p", "proj_pipeline": "q"},
    )
    assert (version_id, current) == (5, False)
    assert any("UPDATE registry_versions" in sql for sql, _ in conn.executed)


def test_prior_load_reads_the_published_version_counts():
    conn = _FakeConn([("SELECT row_counts, proj_pipeline", (
        {"address_points": 3_000_000, "missing_psc": 0, "missing_coords": 900,
         "krovak_y_min": 432_064.28, "krovak_y_max": 901_942.0,
         "krovak_x_min": 936_371.33, "krovak_x_max": 1_219_794.01, "product_skew": 4},
        "S-JTSK to WGS 84 (5)",
    ))])
    prior = ruian_load.prior_load(conn)
    assert prior is not None
    assert prior.row_count == 3_000_000
    assert prior.proj_pipeline == "S-JTSK to WGS 84 (5)"


def test_prior_load_is_none_on_a_first_ever_load():
    assert ruian_load.prior_load(_FakeConn()) is None


def test_prior_load_ignores_a_version_that_never_reached_the_assert_phase():
    conn = _FakeConn([("SELECT row_counts, proj_pipeline", ({"_phase": "staged"}, "p"))])
    assert ruian_load.prior_load(conn) is None


def test_publish_is_a_two_statement_pointer_swap():
    conn = _FakeConn()
    ruian_load.publish(conn, 9)
    statements = [sql for sql, _ in conn.executed]
    assert statements[0].startswith("UPDATE registry_versions SET is_current = false")
    assert statements[1].startswith("UPDATE registry_versions SET is_current = true")


def test_progress_checkpoints_accumulate_without_new_ddl():
    conn = _FakeConn([("SELECT row_counts FROM registry_versions", ({"address_points": 1},))])
    loader_db.write_progress(conn, 3, phase="staged", counts={"staged_chain": 7})
    written = json.loads(conn.executed[-1][1][0])
    assert written["_phase"] == "staged"
    assert written["_phases_done"] == ["staged"]
    assert written["staged_chain"] == 7
    assert written["address_points"] == 1
    assert loader_db.phase_done(written, "staged")
    assert not loader_db.phase_done(written, "points")


def test_abort_records_the_failed_assertion_and_raises():
    conn = _FakeConn()
    with pytest.raises(loader_db.LoadAborted):
        loader_db.abort(conn, 4, reason="assertion_failed",
                        detail={"assertion": "golden_point", "expected": "5 m", "actual": "900 m"})
    sql, params = conn.executed[-1]
    assert "registry_load_discrepancies" in sql
    assert params[3] == "load_aborted"
    detail = json.loads(params[4])
    assert detail["assertion"] == "golden_point"


def test_staging_relations_are_named_per_version():
    stage = ruian_load.Staging.for_version(12)
    assert stage.adr == "ruian_stage_adr_v12"
    assert all(name.endswith("_v12") for name in stage.names().values())
    assert len(set(stage.names().values())) == len(stage.names())


def test_dry_run_produces_every_statistic_the_assertions_need(ob_adr_zip: Path):
    stats = ruian_load.dry_run_stats(ob_adr_zip)
    assert stats.row_count == 4
    assert stats.missing_psc == 0
    # one row has no ordinates, one has sign-flipped (out of envelope) ordinates
    assert stats.missing_coords == 2
    assert stats.golden_distance_m is not None
    assert stats.golden_distance_m <= 5.0
    assert stats.krovak_y_min == 700_000.0
    assert stats.lat_min is not None and 48.0 <= stats.lat_min <= 51.5


def test_dry_run_of_the_fixture_only_fails_the_growth_free_sanity_bound(ob_adr_zip: Path):
    stats = ruian_load.dry_run_stats(ob_adr_zip)
    failures = {
        a.name
        for a in load_assertions.blocking_failures(
            load_assertions.evaluate(stats, None, proj_pipeline="S-JTSK to WGS 84 (5)")
        )
    }
    assert failures == {"row_count_sanity"}


def test_stats_to_counts_round_trips_into_prior_load(ob_adr_zip: Path):
    counts = ruian_load.stats_to_counts(ruian_load.dry_run_stats(ob_adr_zip))
    assert set(counts) >= {
        "address_points", "missing_psc", "missing_coords",
        "krovak_y_min", "krovak_x_max", "product_skew", "golden_point_error_m",
    }
    assert json.loads(json.dumps(counts, default=str))["address_points"] == 4
