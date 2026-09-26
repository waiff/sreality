"""Load orchestration: version bookkeeping, checkpointing, and the DB-free dry run.

The fake connection here proves control flow and SQL shape only — it cannot enforce a
CHECK, a UNIQUE or an FK, so nothing in this file claims the mirror accepted a row.
"""

from __future__ import annotations

import datetime
import hashlib
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


def test_create_version_inserts_one_version_for_both_products():
    conn = _FakeConn([("INSERT INTO registry_versions", (77,))])
    artifacts = {"csv_ob_adr": _artifact("ob"), "csv_strukt_adr": _artifact("strukt")}
    version_id = ruian_load.create_version(
        conn, datetime.date(2026, 7, 31), artifacts,
        {"proj_version": "PROJ 9", "proj_pipeline": "S-JTSK to WGS 84 (5)"},
    )
    assert version_id == 77
    [(insert, params)] = conn.executed
    assert insert.startswith("INSERT INTO registry_versions") and "is_current" in insert
    assert params[0] == "ruian:2026-07-31"
    urls = json.loads(params[2])
    assert set(urls) == {"csv_ob_adr", "csv_strukt_adr"}


def test_create_version_records_the_r2_archive_keys_alongside_the_source_urls():
    conn = _FakeConn([("INSERT INTO registry_versions", (3,))])
    ruian_load.create_version(
        conn, datetime.date(2026, 7, 31), {"csv_ob_adr": _artifact("ob")},
        {"proj_version": "p", "proj_pipeline": "q"},
        archive_keys={"csv_ob_adr_archive": "backups/ruian-archive/ruian:2026-07-31/ob.zip"},
    )
    urls = json.loads(conn.executed[-1][1][2])
    assert urls["csv_ob_adr_archive"].startswith("backups/ruian-archive/")


def test_a_version_recorded_without_an_archived_pack_cannot_be_resumed(tmp_path):
    """A version from before the pack was a vintage artifact (v1, v2) has no archived
    `shp_stat`; resuming it would have nothing to load boundaries from."""
    from location_data import archive

    recorded = ruian_load.Recorded(
        1, False, {"csv_ob_adr_archive": "k/ob.zip", "csv_strukt_adr_archive": "k/st.zip"},
        {}, {}, {}, {},
    )
    with pytest.raises(archive.ArchiveError, match="shp_stat"):
        ruian_load.restore_artifacts(recorded, datetime.date(2026, 7, 31), tmp_path)


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


def test_the_phases_run_in_the_one_full_order():
    """stage -> assertions -> merge -> boundaries -> gazetteer (once) -> completeness ->
    publish -> the MF cells refresh. The gazetteer follows the pack because the pack is the
    only name source for most levels; publish follows the completeness assertion, so a
    current version is complete by construction."""
    source = inspect.getsource(ruian_load.run)
    order = [
        'phase_done(progress, "staged")',
        "load_assertions.evaluate(stats, prior",
        'phase_done(progress, "units")',
        'phase_done(progress, "points")',
        "ruian_boundaries.load_pack(",
        "name_index.rebuild(",
        "ruian_boundaries.missing_geometry(",
        "publish(conn, version_id)",
    ]
    positions = [source.index(step) for step in order]
    assert positions == sorted(positions)
    assert source.rindex("finish(conn, version_id") > positions[-1]
    finish = inspect.getsource(ruian_load.finish)
    assert finish.index("drop_staging(") < finish.index("refresh_rent_map_cells(conn)")
    assert source.count("name_index.rebuild(") == 1
    # The boundaries phase no longer rebuilds the gazetteer on its own.
    from location_data import ruian_boundaries

    assert "name_index.rebuild" not in inspect.getsource(ruian_boundaries)


def test_the_vintage_is_archived_before_it_is_recorded_or_staged():
    """04 §C1.8: a version that was never archived stops being reproducible the moment
    ČÚZK rotates the CSV directory or refreshes the pack, so the upload gates the load —
    with no bypass — and the version row that names the archive keys follows it."""
    source = inspect.getsource(ruian_load.run)
    assert (source.index("archive.archive_version(")
            < source.index("create_version(")
            < source.index("create_staging("))
    assert "allow_unarchived" not in source


def test_the_boundary_pack_is_the_vintages_third_artifact(tmp_path, monkeypatch):
    """Downloaded, hashed, archived and recorded like the two CSV zips; saved under its
    artifact name because its URL carries no vintage."""
    from location_data import ruian_boundaries, ruian_csv

    fetched: list[tuple[str, str, Path]] = []

    def _download(sess, name, url, dest):
        fetched.append((name, url, dest))
        return _artifact(name)

    monkeypatch.setattr(ruian_csv, "download", _download)
    artifacts = ruian_load.fetch_artifacts(object(), datetime.date(2026, 8, 31), tmp_path,
                                           reuse=False)
    assert set(artifacts) == {"csv_ob_adr", "csv_strukt_adr", "shp_stat"}
    pack = next(f for f in fetched if f[0] == "shp_stat")
    assert pack[1] == ruian_boundaries.STATE_PACK_URL
    assert pack[2] == tmp_path / "ruian_shp_stat.zip"


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


# --- the one `full` load, end to end over faked phases ------------------------------------


class _Load:
    """`ruian_load.run` over ONE version row that survives between dispatches, exactly as
    `registry_versions` does, with ČÚZK and R2 faked at their byte-moving edges — the real
    `fetch_artifacts`, `archive_version`, `restore_artifacts` and sha check run."""

    def __init__(self, monkeypatch, tmp_path: Path):
        self.tmp_path = tmp_path
        self.row: dict | None = None      # the registry_versions row, once created
        self.cuzk = {"csv_ob_adr": b"ob", "csv_strukt_adr": b"strukt", "shp_stat": b"pack-a"}
        self.r2: dict[str, bytes] = {}
        self.missing: list = []           # what missing_geometry answers
        self.kill_in_boundaries = False
        self.packs_loaded: list[bytes] = []
        self.calls: dict[str, int] = {}
        self.conn = _FakeConn()
        self.conn.close = lambda: None

        def hit(name, result=None):
            def _call(*a, **k):
                self._count(name)
                return result
            return _call

        from location_data import archive, krovak, name_index, ruian_boundaries, ruian_csv

        monkeypatch.setattr(ruian_csv, "session", lambda: object())
        monkeypatch.setattr(ruian_csv, "download", self._download)
        monkeypatch.setattr(archive, "open_store", lambda: self)
        monkeypatch.setattr(krovak, "proj_environment",
                            lambda: {"proj_version": "p", "proj_pipeline": "q"})
        monkeypatch.setattr(loader_db, "open_loader_connection", lambda: self.conn)
        monkeypatch.setattr(loader_db, "read_progress",
                            lambda conn, v: dict(self.row["progress"]))
        monkeypatch.setattr(loader_db, "write_progress", self._write_progress)
        monkeypatch.setattr(ruian_load, "recorded_version", self._recorded)
        monkeypatch.setattr(ruian_load, "create_version", self._create)
        monkeypatch.setattr(ruian_load, "prior_load", lambda *a, **k: None)
        for name in ("create_staging", "truncate_staging", "index_staging", "drop_staging",
                     "record_product_skew", "upsert_units", "upsert_relations", "report"):
            monkeypatch.setattr(ruian_load, name, hit(name))
        monkeypatch.setattr(ruian_load, "copy_address_points", hit("copy_address_points", {}))
        monkeypatch.setattr(ruian_load, "copy_strukt", hit("copy_strukt", {}))
        monkeypatch.setattr(ruian_load, "gather_stats", hit("gather_stats"))
        monkeypatch.setattr(ruian_load, "stats_to_counts", lambda stats: {})
        monkeypatch.setattr(load_assertions, "evaluate", lambda *a, **k: [])
        monkeypatch.setattr(ruian_load, "build_unit_rows", hit("build_unit_rows", 0))
        monkeypatch.setattr(ruian_load, "upsert_streets", hit("upsert_streets", 0))
        monkeypatch.setattr(ruian_load, "unloadable_rows", lambda *a: 0)
        monkeypatch.setattr(ruian_load, "load_address_points", hit("load_address_points", {}))
        monkeypatch.setattr(ruian_boundaries, "load_pack", self._load_pack)
        monkeypatch.setattr(name_index, "rebuild", hit("gazetteer", 5))
        monkeypatch.setattr(ruian_boundaries, "missing_geometry", lambda conn, v: self.missing)
        monkeypatch.setattr(ruian_load, "publish", self._publish)
        monkeypatch.setattr(ruian_load, "refresh_rent_map_cells", hit("refresh_rent_map_cells"))

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    # ČÚZK
    def _download(self, sess, name, url, dest):
        from location_data.ruian_csv import Artifact

        self._count("cuzk_download")
        dest.write_bytes(self.cuzk[name])
        return Artifact(name=name, url=url, path=dest, bytes=len(self.cuzk[name]),
                        sha256=hashlib.sha256(self.cuzk[name]).hexdigest(),
                        etag=f'"{name}"', last_modified="Thu, 01 Oct 2026 08:00:00 GMT")

    # R2 (archive.ObjectStore)
    def upload_file(self, key, path, content_type="application/zip"):
        self.r2[key] = Path(path).read_bytes()

    def upload_bytes(self, key, data, content_type="application/json"):
        self.r2[key] = data

    def download_file(self, key, path):
        self._count("r2_restore")
        Path(path).write_bytes(self.r2[key])

    # registry_versions
    def _recorded(self, conn, label):
        if self.row is None:
            return None
        return ruian_load.Recorded(7, self.row["current"], *(
            dict(self.row[k]) for k in ("urls", "sha", "bytes", "etag", "lm")))

    def _create(self, conn, vintage, artifacts, proj, *, archive_keys=None):
        assert self.row is None, "a resume must never create a second version row"
        self.row = {"current": False, "progress": {},
                    "urls": {**{n: a.url for n, a in artifacts.items()}, **archive_keys},
                    "sha": {n: a.sha256 for n, a in artifacts.items()},
                    "bytes": {n: a.bytes for n, a in artifacts.items()},
                    "etag": {n: a.etag for n, a in artifacts.items()},
                    "lm": {n: a.last_modified for n, a in artifacts.items()}}
        return 7

    def _write_progress(self, conn, version_id, *, phase=None, counts=None):
        done = self.row["progress"].setdefault("_phases_done", [])
        if phase and phase not in done:
            done.append(phase)

    def _load_pack(self, conn, pack, version_id):
        self._count("boundaries")
        self.packs_loaded.append(pack.read_bytes())
        if self.kill_in_boundaries:
            raise RuntimeError("runner killed mid-pack")
        return {"loaded": 1, "carried": 0}, conn

    def _publish(self, conn, version_id):
        self._count("publish")
        self.row["current"] = True

    def dispatch(self) -> int:
        work_dir = self.tmp_path / f"run{sum(self.calls.values())}"
        work_dir.mkdir()
        return ruian_load.run(vintage=datetime.date(2026, 9, 30), work_dir=work_dir,
                              dry_run=False, reuse=False, keep_staging=False, limit=None)


def test_a_run_killed_mid_boundaries_resumes_and_publishes_exactly_once(monkeypatch, tmp_path):
    load = _Load(monkeypatch, tmp_path)
    load.kill_in_boundaries = True
    with pytest.raises(RuntimeError):
        load.dispatch()
    assert load.calls.get("publish", 0) == 0 and not load.row["current"]
    assert load.calls["copy_address_points"] == 1

    load.kill_in_boundaries = False
    assert load.dispatch() == 0
    assert load.calls["publish"] == 1 and load.row["current"]
    assert load.calls["refresh_rent_map_cells"] == 1
    # The resume re-enters the pack (it skips committed units itself) but not the
    # checkpointed CSV phases.
    assert load.calls["boundaries"] == 2
    assert load.calls["copy_address_points"] == 1
    assert load.calls["load_address_points"] == 1

    # A third dispatch of a current vintage downloads nothing and publishes nothing; it
    # only redoes the steps after the pointer swap.
    assert load.dispatch() == 0
    assert load.calls["publish"] == 1 and load.calls["cuzk_download"] == 3
    assert load.calls["r2_restore"] == 3
    assert load.calls["refresh_rent_map_cells"] == 2
    assert load.calls["drop_staging"] == 2


def test_a_resume_after_the_pack_was_refreshed_loads_the_archived_pack(monkeypatch, tmp_path):
    """ČÚZK refreshes the pack in place daily, so a resume on a later day would download
    other bytes than the version was started from. It never asks ČÚZK: it restores the
    vintage's archive and publishes from the recorded pack."""
    load = _Load(monkeypatch, tmp_path)
    load.kill_in_boundaries = True
    with pytest.raises(RuntimeError):
        load.dispatch()
    recorded_sha = load.row["sha"]["shp_stat"]

    load.cuzk["shp_stat"], load.kill_in_boundaries = b"pack-b", False
    assert load.dispatch() == 0
    assert load.calls["cuzk_download"] == 3            # the first dispatch's three only
    assert load.calls["r2_restore"] == 3
    assert load.packs_loaded == [b"pack-a", b"pack-a"]
    assert load.calls["publish"] == 1 and load.row["current"]
    assert load.row["sha"]["shp_stat"] == recorded_sha


def test_an_archive_that_no_longer_holds_the_recorded_bytes_refuses(monkeypatch, tmp_path):
    load = _Load(monkeypatch, tmp_path)
    load.kill_in_boundaries = True
    with pytest.raises(RuntimeError):
        load.dispatch()

    load.r2[load.row["urls"]["shp_stat_archive"]] = b"pack-x"
    load.kill_in_boundaries = False
    with pytest.raises(loader_db.LoadAborted):
        load.dispatch()
    sql, params = load.conn.executed[-1]
    assert "registry_load_discrepancies" in sql and params[3] == "load_aborted"
    detail = json.loads(params[4])
    assert detail["reason"] == "archive_corrupt"
    assert set(detail["artifacts"]) == {"shp_stat"}
    assert load.calls["boundaries"] == 1 and load.calls.get("publish", 0) == 0


def test_an_incomplete_version_is_never_published(monkeypatch, tmp_path):
    """A member unit without its pip + authoritative rows — a `boundary_load_failed` one
    included — holds the version staged; the next dispatch retries and publishes."""
    load = _Load(monkeypatch, tmp_path)
    load.missing = [("katastralni_uzemi", 2, [600016, 600024])]
    with pytest.raises(loader_db.LoadAborted):
        load.dispatch()
    assert load.calls.get("publish", 0) == 0
    assert load.calls.get("refresh_rent_map_cells", 0) == 0
    sql, params = load.conn.executed[-1]
    assert "registry_load_discrepancies" in sql
    detail = json.loads(params[4])
    assert detail["assertion"] == "boundary_completeness"
    assert "katastralni_uzemi 2" in detail["actual"]

    load.missing = []
    assert load.dispatch() == 0
    assert load.calls["publish"] == 1
