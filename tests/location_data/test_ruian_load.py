"""Load orchestration: version bookkeeping, checkpointing, and the DB-free dry run.

The fake connection here proves control flow and SQL shape only — it cannot enforce a
CHECK, a UNIQUE or an FK, so nothing in this file claims the mirror accepted a row.
"""

from __future__ import annotations

import datetime
import inspect
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
    conn = _FakeConn([("SELECT id, is_current, artifact_sha256", None),
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


def test_ensure_version_records_the_r2_archive_keys_alongside_the_source_urls():
    conn = _FakeConn([("SELECT id, is_current, artifact_sha256", None),
                      ("INSERT INTO registry_versions", (3,))])
    ruian_load.ensure_version(
        conn, datetime.date(2026, 7, 31), {"csv_ob_adr": _artifact("ob")},
        {"proj_version": "p", "proj_pipeline": "q"},
        archive_keys={"csv_ob_adr_archive": "backups/ruian-archive/ruian:2026-07-31/ob.zip"},
    )
    urls = json.loads(conn.executed[-1][1][2])
    assert urls["csv_ob_adr_archive"].startswith("backups/ruian-archive/")


def test_ensure_version_resumes_an_unpublished_load():
    conn = _FakeConn([("SELECT id, is_current, artifact_sha256",
                       (5, False, {"csv_ob_adr": "abc"}, {"csv_ob_adr": 10}))])
    version_id, current = ruian_load.ensure_version(
        conn, datetime.date(2026, 7, 31),
        {"csv_ob_adr": _artifact("ob")},
        {"proj_version": "p", "proj_pipeline": "q"},
    )
    assert (version_id, current) == (5, False)
    assert any("UPDATE registry_versions" in sql for sql, _ in conn.executed)


def test_a_republished_vintage_aborts_instead_of_overwriting_the_record():
    """ČÚZK re-cutting a vintage under the same stamp must not silently rewrite the sha256
    the version's audit trail depends on."""
    conn = _FakeConn([("SELECT id, is_current, artifact_sha256",
                       (5, False, {"csv_ob_adr": "OTHER"}, {"csv_ob_adr": 10}))])
    with pytest.raises(loader_db.LoadAborted):
        ruian_load.ensure_version(
            conn, datetime.date(2026, 7, 31), {"csv_ob_adr": _artifact("ob")},
            {"proj_version": "p", "proj_pipeline": "q"},
        )
    assert any("load_aborted" in str(params) for _, params in conn.executed)
    assert not any(sql.startswith("UPDATE registry_versions SET artifact_urls")
                   for sql, _ in conn.executed)


def test_republished_bytes_can_be_adopted_deliberately():
    conn = _FakeConn([("SELECT id, is_current, artifact_sha256",
                       (5, False, {"csv_ob_adr": "OTHER"}, {"csv_ob_adr": 10}))])
    assert ruian_load.ensure_version(
        conn, datetime.date(2026, 7, 31), {"csv_ob_adr": _artifact("ob")},
        {"proj_version": "p", "proj_pipeline": "q"}, allow_republished=True,
    ) == (5, False)


def test_artifact_mismatch_detection_covers_sha_and_bytes():
    artifacts = {"a": _artifact("a")}  # sha256='abc', bytes=10
    assert ruian_load.artifact_mismatches({"a": "abc"}, {"a": 10}, artifacts) == {}
    assert ruian_load.artifact_mismatches(None, None, artifacts) == {}
    assert "a" in ruian_load.artifact_mismatches({"a": "zzz"}, {"a": 10}, artifacts)
    assert "a" in ruian_load.artifact_mismatches({}, {"a": 11}, artifacts)


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


def test_the_staged_placeholder_flag_is_a_column_the_scd2_predicate_reads():
    """The close predicate must key on the STAGED row's placeholder flag, never on a
    comparison between the mirror's real name and a code placeholder."""
    assert "is_placeholder" in ruian_load._STAGE_DDL
    assert "NOT s.is_placeholder AND u.name IS DISTINCT FROM s.name" in ruian_load._UNIT_CHANGED

    conn = _FakeConn()
    ruian_load.upsert_units(conn, ruian_load.Staging.for_version(1), 1,
                            datetime.date(2026, 7, 31))
    closes = [sql for sql, _ in conn.executed if sql.startswith("UPDATE ruian_admin_units u SET valid_to")]
    inserts = [sql for sql, _ in conn.executed if sql.startswith("INSERT INTO ruian_admin_units")]
    assert len(closes) == len(ruian_load.LEVEL_ORDER)
    assert len(inserts) == len(ruian_load.LEVEL_ORDER)
    flat = " ".join(ruian_load._UNIT_CHANGED.split())
    assert all(flat in sql for sql in closes)
    assert all("coalesce(prev.name, s.name)" in sql for sql in inserts)


def test_a_boundary_name_upgrade_survives_the_next_identical_baseline():
    """Regression: baseline -> boundary pack upgrades the name -> the SAME baseline again.

    Levels the CSV family never names (kraj, okres, ORP, POU, KÚ, ZSJ) stage as their own
    code. Before this rule, round 2 compared 'Benešov' against the staged placeholder
    '3701', closed the row, and re-opened it named '3701' — every monthly baseline reverting
    every name and rewriting the tree. (CI has no Postgres: `unit_needs_new_version` /
    `resolve_unit_name` are the Python mirrors of the SQL fragments asserted above.)
    """
    staged_name, placeholder, parent_id = "3701", True, 42

    # round 1: nothing in the mirror yet, the placeholder is what lands
    name_after_baseline = ruian_load.resolve_unit_name(
        staged_name=staged_name, staged_is_placeholder=placeholder, previous_name=None,
    )
    assert name_after_baseline == "3701"

    # the boundary pack upgrades it (only ever touches a unit still named after its code)
    mirror_name = "Benešov"

    # round 2: the identical baseline stages the identical placeholder
    assert not ruian_load.unit_needs_new_version(
        mirror_name=mirror_name, mirror_parent_id=parent_id,
        staged_name=staged_name, staged_is_placeholder=placeholder,
        staged_parent_id=parent_id,
    )
    # and if anything else DID force a new version, the real name is carried forward
    assert ruian_load.resolve_unit_name(
        staged_name=staged_name, staged_is_placeholder=placeholder,
        previous_name=mirror_name,
    ) == "Benešov"


def test_a_real_name_change_still_opens_a_new_version():
    assert ruian_load.unit_needs_new_version(
        mirror_name="Stará Ves", mirror_parent_id=1,
        staged_name="Nová Ves", staged_is_placeholder=False, staged_parent_id=1,
    )


def test_a_reparent_opens_a_new_version_even_for_a_placeholder_level():
    assert ruian_load.unit_needs_new_version(
        mirror_name="Benešov", mirror_parent_id=1,
        staged_name="3701", staged_is_placeholder=True, staged_parent_id=2,
    )


def test_the_insert_carries_the_existing_name_forward_for_placeholder_levels():
    from location_data.ruian_load import _UNIT_NAME, _UNIT_NAME_NORM

    assert "coalesce(prev.name, s.name)" in _UNIT_NAME
    assert "coalesce(prev.name_norm, s.name_norm)" in _UNIT_NAME_NORM


def test_publish_is_a_two_statement_pointer_swap():
    conn = _FakeConn()
    ruian_load.publish(conn, 9)
    statements = [sql for sql, _ in conn.executed]
    # The guard is statement zero: the loader's session runs statement_timeout = 0 for
    # COPY, so the pointer swap has to re-arm one for itself.
    assert "set_config('statement_timeout'" in statements[0]
    assert statements[1].startswith("UPDATE registry_versions SET is_current = false")
    assert statements[2].startswith("UPDATE registry_versions SET is_current = true")


def test_publish_arms_a_transaction_local_timeout_from_the_env(monkeypatch):
    """statement_timeout = 0 is right for the bulk phases and only for them. The swap is
    two one-row UPDATEs; hanging there leaves the platform on a stale registry version
    with a fully loaded new one beside it."""
    conn = _FakeConn()
    ruian_load.publish(conn, 9)
    guard, params = conn.executed[0]
    assert params["statement_timeout"] == f"{ruian_load.DEFAULT_PUBLISH_TIMEOUT_S}s"
    assert params["lock_timeout"] == "5s"
    # `true` is the is_local flag — SET LOCAL, so it reverts at commit and the next COPY
    # is not silently clamped by it.
    assert guard.count("true") == 2

    monkeypatch.setenv(ruian_load.PUBLISH_TIMEOUT_ENV, "17")
    conn = _FakeConn()
    ruian_load.publish(conn, 9)
    assert conn.executed[0][1]["statement_timeout"] == "17s"


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


def test_the_gazetteer_is_rebuilt_before_the_pointer_swap():
    """Publishing first and dying mid-rebuild leaves the version every resolution binds to
    with ZERO ruian_name_index rows — and a resume gated on is_current could never fix it."""
    source = inspect.getsource(ruian_load.run)
    assert source.index("name_index.rebuild") < source.index("publish(conn, version_id)")
    assert 'phase_done(progress, "gazetteer")' in source
    assert 'already_current and loader_db.phase_done(progress, "published")' in source


def test_the_vintage_is_archived_before_anything_is_staged():
    """04 §C1.8: a version that was never archived stops being reproducible the moment
    ČÚZK rotates the CSV directory, so the upload gates the load."""
    source = inspect.getsource(ruian_load.run)
    assert source.index("archive.archive_version") < source.index("open_loader_connection")
    assert "allow_unarchived" in source


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
