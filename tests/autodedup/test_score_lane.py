"""The score lane end to end against fakes — no network, no database, no artifact download.

Every seam the lane owns is exercised here: the run row's two terminal paths, the
`autodedup.pairs` column contract of migration 528, the generation rebuilt whole
(delete-then-insert) rather than merged into, the store floor, the operator must-not-link
read that binds the pass, and the summary the progress ledger files.

The cohort is the synthetic one from `test_engine_e2e` — the same six shapes the engine's own
end-to-end test argues about — so a change that keeps this file green while breaking the
engine still fails next door.
"""

from __future__ import annotations

import gzip
import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from autodedup import lane as lane_module
from autodedup import score_lane
from autodedup.score_sql import (
    CLUSTER_CONFLICT_INSERT_SQL,
    CLUSTER_CONFLICTS_DELETE_SQL,
    CLUSTER_INSERT_SQL,
    CLUSTER_MEMBER_INSERT_SQL,
    CLUSTER_MEMBERS_DELETE_SQL,
    CLUSTERS_DELETE_SQL,
    GENERATIONS_SQL,
    JUDGED_EDGES_SQL,
    MUST_NOT_LINK_SQL,
    PAIR_CLUSTER_ORPHAN_CLEAR_SQL,
    PAIR_UPSERT_SQL,
    PRUNE_CLUSTERS_SQL,
    PRUNE_CONFLICTS_SQL,
    PRUNE_MEMBERS_SQL,
    PRUNE_PAIRS_SQL,
    RUN_FINISH_SQL,
    RUN_START_SQL,
    STORE_PRESENT_SQL,
)
from autodedup.incremental_sql import RT_SCHEMA_SIZE_SQL
from tests.autodedup.test_engine_e2e import DUP_A, DUP_B, build_records

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
MIGRATION = MIGRATIONS / "528_autodedup_foundation.sql"
BLOCK_GRAIN_MIGRATION = MIGRATIONS / "529_autodedup_cluster_block_grain.sql"
GENERATION_MIGRATION = MIGRATIONS / "538_autodedup_generation_scoped_store.sql"
REALTIME_MIGRATION = MIGRATIONS / "539_autodedup_realtime_lane.sql"
# Columns migration 539 added for the REAL-TIME lane's own bookkeeping: E71's two retrieval
# booleans, the strings a rule refused on, the census a promotion was taken under, the two
# fingerprint digests an idempotent re-score compares and the frozen calibration's digest. The
# batch pass has no equivalent of any of them. `certificate` is deliberately NOT here: the
# clustering ORDERS on it, so both lanes write it (D41).
REALTIME_ONLY = {"from_lo", "from_hi", "evidence", "context", "fp_lo", "fp_hi",
                 "calibration_digest"}

RUN_ID = 77

# Transaction boundaries the fake connection records inline, so a test can ask what landed
# together. Distinct objects: the helpers below compare statements by identity.
BEGIN = "-- begin"
COMMIT = "-- commit"
ROLLBACK = "-- rollback"


# --- fakes -------------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((sql, params))
        if sql in self._conn.raises:
            raise RuntimeError(self._conn.raises[sql])
        if sql is STORE_PRESENT_SQL:
            self._rows = [(self._conn.store_ready,)]
        elif sql is RUN_START_SQL:
            self._rows = [(RUN_ID,)]
        elif sql is MUST_NOT_LINK_SQL:
            self._rows = list(self._conn.must_not_link)
        elif sql is JUDGED_EDGES_SQL:
            self._rows = list(self._conn.judged)
        elif sql is GENERATIONS_SQL:
            self._rows = list(self._conn.generations)
        elif sql is RT_SCHEMA_SIZE_SQL:
            self._rows = self._conn.schema_size()
        else:
            self._rows = []

    def executemany(self, sql: str, params: Any = None) -> None:
        rows = list(params or ())
        self._conn.executed.append((sql, rows))
        if sql in self._conn.raises:
            raise RuntimeError(self._conn.raises[sql])

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, executed: list[tuple[str, Any]], state: dict[str, Any]) -> None:
        self.executed = executed
        self.store_ready = bool(state.get("store_ready", True))
        self.must_not_link = list(state.get("must_not_link", ()))
        self.judged = list(state.get("judged", ()))
        self.generations = list(state.get("generations", ()))
        self.raises: dict[str, str] = dict(state.get("raises", {}))
        # What `pg_total_relation_size` over the schema reads, one value per read in order and
        # the last one repeated; absent = the catalog answers nothing.
        self.schema_bytes: list[int] = state.setdefault("schema_bytes", [])
        self.closed = False

    def schema_size(self) -> list[tuple[int]]:
        if not self.schema_bytes:
            return []
        value = self.schema_bytes.pop(0) if len(self.schema_bytes) > 1 else self.schema_bytes[0]
        return [(value,)]

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.executed.append((BEGIN, None))
        try:
            yield
        except BaseException:
            self.executed.append((ROLLBACK, None))
            raise
        self.executed.append((COMMIT, None))

    def close(self) -> None:
        self.closed = True


@pytest.fixture(scope="module")
def cohort(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("cohort") / "cohort.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for record in build_records():
            handle.write(json.dumps(record) + "\n")
    return path


@pytest.fixture()
def lane(monkeypatch: pytest.MonkeyPatch, cohort: Path):
    """`lane(out, **args) -> summary`, with `.executed`, `.state` and `.conns` attached."""
    executed: list[tuple[str, Any]] = []
    conns: list[FakeConn] = []
    state: dict[str, Any] = {"downloads": []}

    def fake_download(export_run: str, dest: Path) -> Path:
        state["downloads"].append((export_run, Path(dest)))
        Path(dest).mkdir(parents=True, exist_ok=True)
        target = Path(dest) / score_lane.COHORT_FILE
        target.write_bytes(cohort.read_bytes())
        return target

    monkeypatch.setattr(score_lane, "download_cohort", fake_download)

    def factory() -> FakeConn:
        conn = FakeConn(executed, state)
        conns.append(conn)
        return conn

    def run(out: Path, **args: Any) -> dict[str, Any]:
        raw = {key: str(value) for key, value in args.items()}
        return score_lane.run_score(factory, raw, Path(out))

    run.executed = executed  # type: ignore[attr-defined]
    run.state = state  # type: ignore[attr-defined]
    run.conns = conns  # type: ignore[attr-defined]
    return run


def _params(executed: list[tuple[str, Any]], sql: str) -> list[Any]:
    return [params for statement, params in executed if statement is sql]


def _rows(executed: list[tuple[str, Any]], sql: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for batch in _params(executed, sql):
        out.extend(batch)
    return out


def _index(executed: list[tuple[str, Any]], sql: str) -> int:
    for position, (statement, _) in enumerate(executed):
        if statement is sql:
            return position
    return -1


# --- arguments ---------------------------------------------------------------------------


def test_an_unknown_argument_is_refused_before_any_work(lane, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        lane(tmp_path / "out", export_run="1", tiers="text")
    assert "tiers" in str(exc.value)
    assert not lane.executed


@pytest.mark.parametrize("bad", [{}, {"export_run": "nope"}, {"export_run": "1",
                                                             "store_floor": "x"}])
def test_bad_arguments_are_refused(lane, tmp_path: Path, bad: dict) -> None:
    with pytest.raises(SystemExit):
        lane(tmp_path / "out", **bad)
    assert not lane.executed


def test_a_settings_name_must_live_in_the_repo(lane, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        lane(tmp_path / "out", export_run="1", settings="../../etc/passwd")
    assert "settings" in str(exc.value)


def test_the_export_run_is_downloaded(lane, tmp_path: Path) -> None:
    lane(tmp_path / "out", export_run="4242")
    assert lane.state["downloads"] and lane.state["downloads"][0][0] == "4242"


# --- the run row -------------------------------------------------------------------------


def test_the_pass_opens_one_run_row_and_closes_it_success(lane, tmp_path: Path) -> None:
    summary = lane(tmp_path / "out", export_run="1", generation="gtest")
    start = _params(lane.executed, RUN_START_SQL)
    assert len(start) == 1
    assert set(start[0]) == {"fingerprint", "params"}
    assert len(start[0]["fingerprint"]) == 64
    params = json.loads(start[0]["params"])
    assert params["generation"] == "gtest" and params["export_run"] == "1"

    finish = _params(lane.executed, RUN_FINISH_SQL)
    assert len(finish) == 1
    assert finish[0]["status"] == "success"
    assert finish[0]["error"] is None
    assert finish[0]["id"] == RUN_ID
    # The cohort is only knowable once the dataset is loaded, so it lands on the close.
    cohort = json.loads(finish[0]["cohort"])
    assert cohort["export_run"] == "1" and cohort["blocks"]
    assert json.loads(finish[0]["stats"])["pairs_scored"] > 0
    assert summary["run_id"] == RUN_ID
    assert all(conn.closed for conn in lane.conns)


def test_the_run_row_is_open_before_the_engine_runs(
    lane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine is the long phase and the one that dies; a row opened after it would make
    exactly that failure invisible."""
    real = score_lane.harness.run_engine
    seen: list[int] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(len(lane.executed))
        return real(*args, **kwargs)

    monkeypatch.setattr(score_lane.harness, "run_engine", spy)
    lane(tmp_path / "out", export_run="1")
    start = _index(lane.executed, RUN_START_SQL)
    assert start >= 0 and start < seen[0]


def test_an_engine_crash_still_leaves_a_failed_run_row(
    lane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise MemoryError("cohort does not fit")

    monkeypatch.setattr(score_lane.harness, "run_engine", boom)
    with pytest.raises(MemoryError):
        lane(tmp_path / "out", export_run="1")
    finish = _params(lane.executed, RUN_FINISH_SQL)
    assert len(finish) == 1 and finish[0]["status"] == "failed"
    assert "cohort does not fit" in finish[0]["error"]
    assert finish[0]["cohort"] is None and finish[0]["stats"] is None


def test_a_failing_write_marks_the_run_failed_and_re_raises(lane, tmp_path: Path) -> None:
    lane.state["raises"] = {PAIR_UPSERT_SQL: "relation autodedup.pairs does not exist"}
    with pytest.raises(RuntimeError):
        lane(tmp_path / "out", export_run="1")
    finish = _params(lane.executed, RUN_FINISH_SQL)
    assert len(finish) == 1
    assert finish[0]["status"] == "failed"
    assert "autodedup.pairs does not exist" in finish[0]["error"]
    assert all(conn.closed for conn in lane.conns)


def test_the_error_text_is_scrubbed_before_it_is_stored(
    lane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://user:hunter2@db.example/postgres")
    lane.state["raises"] = {
        PAIR_UPSERT_SQL: "invalid dsn: postgresql://user:hunter2@db.example/postgres"
    }
    with pytest.raises(RuntimeError):
        lane(tmp_path / "out", export_run="1")
    error = _params(lane.executed, RUN_FINISH_SQL)[0]["error"]
    assert "hunter2" not in error and "postgresql://" not in error


def test_a_dead_connection_does_not_replace_the_real_failure(lane, tmp_path: Path) -> None:
    """Bookkeeping is best effort; the exception the operator must see is the first one."""
    lane.state["raises"] = {
        PAIR_UPSERT_SQL: "server closed the connection",
        RUN_FINISH_SQL: "the connection is closed",
    }
    with pytest.raises(RuntimeError, match="server closed the connection"):
        lane(tmp_path / "out", export_run="1")


def test_an_absent_store_refuses_the_pass(lane, tmp_path: Path) -> None:
    lane.state["store_ready"] = False
    with pytest.raises(SystemExit) as exc:
        lane(tmp_path / "out", export_run="1")
    assert "528" in str(exc.value)
    assert not _params(lane.executed, RUN_START_SQL)


# --- the pair contract -------------------------------------------------------------------


def _migration_columns(table: str) -> list[str]:
    body = MIGRATION.read_text(encoding="utf-8")
    block = re.search(
        rf"create table if not exists autodedup\.{table} \((.*?)\n\);", body, re.S
    )
    assert block
    columns: list[str] = []
    for line in block.group(1).splitlines():
        line = line.strip()
        match = re.match(
            r"^([a-z_]+)\s+(bigserial|bigint|integer|smallint|text|real|boolean|numeric|"
            r"uuid|jsonb|timestamptz)",
            line,
        )
        if match and match.group(1) not in ("primary", "constraint"):
            columns.append(match.group(1))
    # Migration 538 adds `generation` to three of these tables; the contract a writer has to
    # satisfy is the CURRENT one, so the added columns are folded in here rather than listed
    # by hand in every test below.
    # Migration 538 adds `generation` to three of these tables and 539 adds the pair columns
    # the real-time lane needs; the contract a writer has to satisfy is the CURRENT one, so
    # every later `add column` is folded in here rather than listed by hand below.
    added: list[str] = []
    for path in (GENERATION_MIGRATION, REALTIME_MIGRATION):
        added += re.findall(
            rf"add column if not exists ([a-z_]+)",
            path.read_text(encoding="utf-8").split(f"alter table autodedup.{table}", 1)[-1]
            .split(";", 1)[0],
        )
    return columns + [name for name in added if name not in columns]


def test_pair_upsert_params_match_migration_528(lane, tmp_path: Path) -> None:
    lane(tmp_path / "out", export_run="1")
    rows = _rows(lane.executed, PAIR_UPSERT_SQL)
    assert rows
    columns = set(_migration_columns("pairs"))
    # `decided_at` is `now()` in the statement; `applied_merge_group` belongs to the write
    # path (E40) and shadow mode never names it; `REALTIME_ONLY` is the other lane's
    # bookkeeping. Everything else the table holds, this lane writes — including
    # `certificate`, which `cluster.edge_rank` reads first (D41, M171).
    assert set(rows[0]) == columns - {"decided_at", "applied_merge_group"} - REALTIME_ONLY
    assert "certificate" in set(rows[0])
    for row in rows:
        assert row["listing_lo"] < row["listing_hi"]
        assert row["zone"] in ("merge", "band", "reject")
        assert isinstance(row["probes"], list) and row["probes"]
        assert 0 <= row["families"] < 128
        assert row["feature_version"] == score_lane.FEATURE_VERSION
        assert row["model_version"]
        assert json.loads(row["features"])


def test_only_present_features_are_stored(lane, tmp_path: Path) -> None:
    lane(tmp_path / "out", export_run="1")
    rows = _rows(lane.executed, PAIR_UPSERT_SQL)
    features = json.loads(rows[0]["features"])
    assert features
    assert all(entry[1] is True for entry in features.values())


def test_the_family_bitmask_is_the_sum_of_its_families() -> None:
    assert score_lane.families_bitmask(["ATTR", "IMG"]) == 1 + 32
    assert score_lane.families_bitmask([]) == 0
    assert score_lane.families_bitmask(["NOPE"]) == 0
    row = {"lo": 2, "hi": 1, "families": ["LOC", "TXT"], "zone": "veto",
           "veto": "deal_conflict", "reason": "guard:deal_conflict", "score": 0.0,
           "probes": ["K1"], "feats": {"dist_norm": [0.1, True], "gap_days": [3.0, False]}}
    params = score_lane.pair_params(row, {}, "hand_v1", "g1")
    # `autodedup_pairs_order_ck` refuses lo >= hi, and one bad row aborts its whole chunk.
    assert (params["listing_lo"], params["listing_hi"]) == (1, 2)
    assert params["families"] == 4 + 16
    # A fourth zone value would violate the table's CHECK; the veto survives in its own column.
    assert params["zone"] == "reject" and params["guard_veto"] == "deal_conflict"
    assert json.loads(params["features"]) == {"dist_norm": [0.1, True]}
    assert params["generation"] == "g1"


def test_the_store_floor_decides_which_tail_is_written(lane, tmp_path: Path) -> None:
    lane(tmp_path / "out1", export_run="1", store_floor="0.3")
    tight = len(_rows(lane.executed, PAIR_UPSERT_SQL))
    lane.executed.clear()
    lane(tmp_path / "out2", export_run="1", store_floor="0")
    wide = len(_rows(lane.executed, PAIR_UPSERT_SQL))
    assert 0 < tight < wide
    assert score_lane.storable({"zone": "reject", "score": 0.1}, 0.3) is False
    assert score_lane.storable({"zone": "band", "score": 0.0}, 0.3) is True


# --- the generation ----------------------------------------------------------------------


def test_a_generation_is_deleted_before_it_is_inserted(lane, tmp_path: Path) -> None:
    lane(tmp_path / "out", export_run="1", generation="gtest")
    assert _params(lane.executed, CLUSTER_CONFLICTS_DELETE_SQL) == [{"generation": "gtest"}]
    # members first: their only link to the generation is the cluster row.
    assert _index(lane.executed, CLUSTER_MEMBERS_DELETE_SQL) < _index(
        lane.executed, CLUSTERS_DELETE_SQL
    )
    for sql in (CLUSTER_INSERT_SQL, CLUSTER_MEMBER_INSERT_SQL):
        assert _index(lane.executed, CLUSTERS_DELETE_SQL) < _index(lane.executed, sql)


def test_the_sweep_never_reaches_outside_its_own_generation(lane, tmp_path: Path) -> None:
    """E58. `clusters` is keyed `(generation, cluster_key)` since migration 538, so this
    pass's own generation is the whole scope — the old key-set disjunct was exactly the
    cross-generation reach that let g5 re-stamp 836 of g4's clusters."""
    lane(tmp_path / "out", export_run="1", generation="g2")
    assert {row["cluster_key"] for row in _rows(lane.executed, CLUSTER_INSERT_SQL)}
    for sql in (CLUSTER_MEMBERS_DELETE_SQL, CLUSTERS_DELETE_SQL):
        assert _params(lane.executed, sql) == [{"generation": "g2"}]
        assert "cluster_key" not in sql
    # Every row and every scope this pass wrote names g2 and nothing else.
    for sql, params in lane.executed:
        if not isinstance(sql, str) or "autodedup." not in sql:
            continue
        if sql in (GENERATIONS_SQL, MUST_NOT_LINK_SQL, JUDGED_EDGES_SQL, STORE_PRESENT_SQL):
            continue
        if not ("clusters" in sql or "cluster_members" in sql or "autodedup.pairs" in sql):
            continue
        for row in (params if isinstance(params, list) else [params]):
            if isinstance(row, dict) and "generation" in row:
                assert row["generation"] == "g2", sql


# --- keep_generations --------------------------------------------------------------------


def test_nothing_is_pruned_unless_the_operator_asks(lane, tmp_path: Path) -> None:
    summary = lane(tmp_path / "out", export_run="1", generation="g9")
    for sql in (PRUNE_MEMBERS_SQL, PRUNE_CLUSTERS_SQL, PRUNE_PAIRS_SQL, PRUNE_CONFLICTS_SQL):
        assert _params(lane.executed, sql) == []
    assert summary["prune"] == {"pruned": [], "kept": None}


def test_keep_generations_drops_the_oldest_and_never_this_pass(lane, tmp_path: Path) -> None:
    lane.state["generations"] = [("g1",), ("g2",), ("g3",), ("g4",)]
    summary = lane(tmp_path / "out", export_run="1", generation="g5", keep_generations="2")
    doomed = _params(lane.executed, PRUNE_CLUSTERS_SQL)[0]["generations"]
    # Oldest first from GENERATIONS_SQL, so g3/g4 survive and the pass's own g5 always does.
    assert doomed == ["g1", "g2"]
    assert "g5" not in doomed
    for sql in (PRUNE_MEMBERS_SQL, PRUNE_PAIRS_SQL, PRUNE_CONFLICTS_SQL):
        assert _params(lane.executed, sql) == [{"generations": ["g1", "g2"]}]
    assert summary["counts"]["prune"]["pruned"] == ["g1", "g2"]
    assert summary["counts"]["prune"]["kept"] == 2
    assert summary["prune"] is summary["counts"]["prune"]
    # The prune runs AFTER the generation it is keeping is on disk.
    assert _index(lane.executed, CLUSTER_INSERT_SQL) < _index(lane.executed, PRUNE_CLUSTERS_SQL)


def test_keep_generations_ranks_by_last_write_oldest_first_not_by_name() -> None:
    """The documented order (PROGRAM.md's g7_rtbase note, `GENERATIONS_SQL`): oldest first by the
    last time a generation's clusters were written, and every upsert stamps that time — so a
    re-scored generation is the newest whatever its name. Name order would drop g9 here."""
    assert "order by max(c.last_changed_at) asc" in GENERATIONS_SQL
    assert "last_changed_at      = now()" in CLUSTER_INSERT_SQL.split("do update set")[1]
    executed: list[tuple[str, Any]] = []
    conn = FakeConn(executed, {"generations": [("g10",), ("g9",), ("g11",)]})
    out = score_lane.prune_generations(conn, keep=2, current="g11")
    assert out["pruned"] == ["g10"]
    assert out["ranked_oldest_first"] == ["g10", "g9", "g11"]
    assert out["survivors"] == ["g9", "g11"]


def test_keep_generations_never_counts_the_live_stream(lane, tmp_path: Path) -> None:
    """E917: the worker's lane rewrites `rt`'s clusters every pass, so `rt` is always the
    newest. Ranked with the batch passes it took one of the `keep` slots, and a re-score of
    g5 with keep=2 dropped g3 as well — two batch generations kept became one."""
    lane.state["generations"] = [("g1",), ("g2",), ("g3",), ("g5",), ("rt",)]
    summary = lane(tmp_path / "out", export_run="1", generation="g5", keep_generations="2")
    prune = summary["prune"]
    assert prune["pruned"] == ["g1", "g2"]
    assert prune["ranked_oldest_first"] == ["g1", "g2", "g3", "g5"]
    assert prune["survivors"] == ["g3", "g5"]
    for sql in (PRUNE_MEMBERS_SQL, PRUNE_CLUSTERS_SQL, PRUNE_PAIRS_SQL, PRUNE_CONFLICTS_SQL):
        assert _params(lane.executed, sql) == [{"generations": ["g1", "g2"]}]


def test_the_summary_reports_what_was_pruned_and_the_schema_either_side(
    lane, tmp_path: Path
) -> None:
    """The 2026-09-26 re-score of g15 with keep_generations=4 left no readable receipt of what
    it dropped or what the schema cost. The summary and `score.json` say both — and that a
    DELETE does not shrink the schema is visible, not a surprise."""
    mb = 1_048_576
    lane.state["generations"] = [("g1",), ("g2",), ("g3",)]
    # pass start, prune before, prune after, pass end
    lane.state["schema_bytes"] = [400 * mb, 440 * mb, 440 * mb, 440 * mb]
    summary = lane(tmp_path / "out", export_run="1", generation="g3", keep_generations="1")
    assert summary["prune"]["pruned"] == ["g1", "g2"]
    assert summary["prune"]["schema_mb_before"] == 440.0
    assert summary["prune"]["schema_mb_after"] == 440.0
    assert summary["storage"] == {"schema_mb_before": 400.0, "schema_mb_after": 440.0}
    on_disk = json.loads((tmp_path / "out" / score_lane.SUMMARY_FILE).read_text())
    assert on_disk["prune"]["pruned"] == ["g1", "g2"]
    assert on_disk["storage"]["schema_mb_before"] == 400.0


def test_keep_generations_refuses_a_value_that_is_not_a_count() -> None:
    for bad in ("0", "-1", "all", "1.5"):
        with pytest.raises(SystemExit) as exc:
            score_lane.parse_args({"export_run": "1", "keep_generations": bad})
        assert "keep_generations" in str(exc.value)
    assert score_lane.parse_args({"export_run": "1"}).keep_generations is None
    assert score_lane.parse_args(
        {"export_run": "1", "keep_generations": "3"}
    ).keep_generations == 3


def test_the_generation_rebuild_is_one_transaction(lane, tmp_path: Path) -> None:
    lane(tmp_path / "out", export_run="1")
    first = _index(lane.executed, CLUSTER_MEMBERS_DELETE_SQL)
    last = _index(lane.executed, PAIR_CLUSTER_ORPHAN_CLEAR_SQL)
    assert 0 <= first < last
    window = [sql for sql, _ in lane.executed[first:last]]
    assert COMMIT not in window and ROLLBACK not in window
    assert lane.executed[first - 1][0] is BEGIN


def test_a_pair_left_pointing_at_a_vanished_cluster_is_unlinked(
    lane, tmp_path: Path
) -> None:
    """`pairs` outlives the generation rebuild, so its cluster_key can dangle."""
    lane(tmp_path / "out", export_run="1")
    assert _params(lane.executed, PAIR_CLUSTER_ORPHAN_CLEAR_SQL) == [{"generation": "g1"}]
    assert _index(lane.executed, PAIR_CLUSTER_ORPHAN_CLEAR_SQL) > _index(
        lane.executed, CLUSTER_INSERT_SQL
    )


def test_cluster_rows_match_migration_528(lane, tmp_path: Path) -> None:
    lane(tmp_path / "out", export_run="1", generation="gtest")
    rows = _rows(lane.executed, CLUSTER_INSERT_SQL)
    assert rows
    columns = set(_migration_columns("clusters")) | {"block_grain"}
    assert set(rows[0]) == columns - {"first_built_at", "last_changed_at", "property_id"}
    for row in rows:
        assert row["generation"] == "gtest"
        assert row["status"] == "proposed"
        assert row["size"] >= 2
        assert row["medoid_listing_id"] is not None
        assert row["n_judged_edges"] == 0
        assert isinstance(row["shared_photo_warning"], bool)
        assert row["feature_version"] == score_lane.FEATURE_VERSION


def test_every_cluster_member_is_written_with_the_edge_it_arrived_on(
    lane, tmp_path: Path
) -> None:
    lane(tmp_path / "out", export_run="1")
    clusters = {row["cluster_key"]: row for row in _rows(lane.executed, CLUSTER_INSERT_SQL)}
    members = _rows(lane.executed, CLUSTER_MEMBER_INSERT_SQL)
    assert len(members) == sum(row["size"] for row in clusters.values())
    for member in members:
        assert member["cluster_key"] in clusters
        assert member["joined_via_lo"] < member["joined_via_hi"]
    assert set(_migration_columns("cluster_members")) - {"joined_at"} == set(members[0])


def test_a_refused_union_is_stored_with_the_generation(lane, tmp_path: Path) -> None:
    lane.state["must_not_link"] = [(DUP_A, DUP_B, "operator")]
    lane(tmp_path / "out", export_run="1", generation="gtest")
    rows = _rows(lane.executed, CLUSTER_CONFLICT_INSERT_SQL)
    assert len(rows) == 1
    assert set(_migration_columns("cluster_conflicts")) - {"id", "created_at"} == set(rows[0])
    row = rows[0]
    assert row["kind"] == "invariant" and row["invariant"] == "must_not_link"
    assert (row["listing_lo"], row["listing_hi"]) == (DUP_A, DUP_B)
    assert json.loads(row["detail"])["generation"] == "gtest"


def test_a_refused_bridge_keeps_both_cluster_keys() -> None:
    clusters = {
        "bridges": [{"lo": 5, "hi": 9, "score": 0.98, "certificate": "K-A",
                     "families": ["IMG"], "left_cluster": 5, "left_members": [5, 6],
                     "right_cluster": 9, "right_members": [9, 10]}],
    }
    rows = score_lane.conflict_params(clusters, {}, "gtest")
    assert len(rows) == 1 and rows[0]["kind"] == "bridge"
    assert (rows[0]["cluster_key_a"], rows[0]["cluster_key_b"]) == (5, 9)
    detail = json.loads(rows[0]["detail"])
    assert detail["generation"] == "gtest" and detail["right_members"] == [9, 10]


def test_a_pair_carries_its_cluster_key_only_when_both_sides_landed_in_it(
    lane, tmp_path: Path
) -> None:
    lane(tmp_path / "out", export_run="1")
    members = {
        (row["cluster_key"], row["listing_id"])
        for row in _rows(lane.executed, CLUSTER_MEMBER_INSERT_SQL)
    }
    keyed = [row for row in _rows(lane.executed, PAIR_UPSERT_SQL)
             if row["cluster_key"] is not None]
    assert keyed
    for row in keyed:
        assert (row["cluster_key"], row["listing_lo"]) in members
        assert (row["cluster_key"], row["listing_hi"]) in members


# --- the reads the pass depends on -------------------------------------------------------


def test_an_operator_must_not_link_row_binds_the_pass(lane, tmp_path: Path) -> None:
    summary = lane(tmp_path / "out1", export_run="1")
    before = {row["cluster_key"] for row in _rows(lane.executed, CLUSTER_INSERT_SQL)}
    assert DUP_A in before and summary["n_must_not_link"] == 0

    lane.executed.clear()
    lane.state["must_not_link"] = [(DUP_A, DUP_B, "operator")]
    summary = lane(tmp_path / "out2", export_run="1")
    assert summary["n_must_not_link"] == 1
    assert _index(lane.executed, MUST_NOT_LINK_SQL) < _index(lane.executed, RUN_START_SQL)
    after = {row["cluster_key"] for row in _rows(lane.executed, CLUSTER_INSERT_SQL)}
    assert DUP_A not in after


def test_judged_edges_are_counted_from_the_judgement_store(lane, tmp_path: Path) -> None:
    lane(tmp_path / "out1", export_run="1")
    edge = next(
        (row["listing_lo"], row["listing_hi"])
        for row in _rows(lane.executed, PAIR_UPSERT_SQL)
        if row["cluster_key"] is not None
    )
    lane.executed.clear()
    lane.state["judged"] = [edge]
    lane(tmp_path / "out2", export_run="1")
    asked = _params(lane.executed, JUDGED_EDGES_SQL)[0]
    assert len(asked["los"]) == len(asked["his"]) and edge[0] in asked["los"]
    rows = _rows(lane.executed, CLUSTER_INSERT_SQL)
    assert sum(row["n_judged_edges"] for row in rows) == 1


# --- artifacts and the ledger ------------------------------------------------------------


def test_the_pass_writes_its_artifacts_and_a_counted_summary(lane, tmp_path: Path) -> None:
    out = tmp_path / "out"
    summary = lane(out, export_run="1", generation="gtest")
    assert (out / score_lane.RUN_FILE).is_file()
    assert (out / score_lane.SUMMARY_FILE).is_file()
    assert (out / "pairs.jsonl.gz").is_file() and (out / "clusters.json").is_file()
    assert json.loads((out / score_lane.RUN_FILE).read_text())["generation"] == "gtest"

    counts = summary["counts"]
    assert counts["pairs_upserted"] == len(_rows(lane.executed, PAIR_UPSERT_SQL))
    assert counts["clusters"] == len(_rows(lane.executed, CLUSTER_INSERT_SQL))
    assert counts["cluster_members"] == len(_rows(lane.executed, CLUSTER_MEMBER_INSERT_SQL))
    assert counts["cluster_conflicts"] == len(
        _rows(lane.executed, CLUSTER_CONFLICT_INSERT_SQL)
    )
    assert counts["zone_merge"] > 0 and "certificate_K-A" in counts
    assert counts["pairs_below_floor"] == counts["pairs_scored"] - counts["pairs_upserted"]
    # `_metrics` is what the progress page puts in its numbers column.
    assert lane_module._metrics(summary)["counts"] is counts
    assert summary["timings"] and summary["clusters_stats"]["n_clusters"] > 0


def test_the_mode_is_registered_with_its_ledger_meta() -> None:
    assert lane_module.MODES["score"] is score_lane.run_score
    meta = lane_module.ITERATION_META["score"]
    assert meta["wave"] == "W5" and meta["title"] and meta["approach"]
    assert "autodedup.score_lane" in meta["tools"]


def test_the_workflow_documents_the_mode() -> None:
    body = (MIGRATION.parents[1] / ".github" / "workflows" / "autodedup.yml").read_text(
        encoding="utf-8"
    )
    assert "#   score" in body
    assert "score accepts export_run, cohort, settings, model, generation" in (
        " ".join(body.split())
    )


def test_the_fingerprint_moves_with_settings_model_and_generation(lane) -> None:
    from autodedup.model import hand_initialised
    from autodedup.settings import Settings

    model = hand_initialised()
    base = score_lane.fingerprint_of(Settings(), model, "g1")
    assert base == score_lane.fingerprint_of(Settings(), model, "g1")
    assert base != score_lane.fingerprint_of(Settings(), model, "g2")
    assert base != score_lane.fingerprint_of(Settings(store_floor=0.05), model, "g1")


def test_the_block_grain_is_stored_beside_the_code_not_dropped() -> None:
    """`clusters.block_key` is a bigint and the engine's key is grain-prefixed text, so the
    letter goes to `block_grain` (migration 529): dropping it made a quarter and a town with
    the same RÚIAN number one filter value."""
    assert score_lane.block_key_of(["c490245"]) == (490245, "c")
    assert score_lane.block_key_of(["o490245"]) == (490245, "o")
    assert score_lane.block_key_of(["o563510", "c490245"]) == (None, None)
    assert score_lane.block_key_of([""]) == (None, None)
    assert score_lane.block_key_of(["x1"]) == (None, None)
    assert "add column if not exists block_grain" in BLOCK_GRAIN_MIGRATION.read_text(
        encoding="utf-8"
    )


def test_the_feature_reader_is_the_harness_definition() -> None:
    assert score_lane.feature_value({"feats": {"gap_days": [3.0, True]}}, "gap_days") == 3.0
    assert score_lane.feature_value({"feats": {"gap_days": [3.0, False]}}, "gap_days") is None


def test_the_shipped_w5_row_loads_and_keeps_model_same_propose_only() -> None:
    """Generation g4's row (PROGRAM.md D16). `model|same` stays propose-only: once
    `28071 x 18661514` is judged at gold no cut in that cell clears the 0.995 point gate, and
    `w5_strata.json` is EVIDENCE, not a settings row — it must not be loadable as one."""
    from autodedup.settings import Settings

    row = Settings.from_json(score_lane.repo_path("w5", score_lane.SETTINGS_DIR))
    assert row.t_hi_by_stratum["model|cross"] == 0.9788
    assert row.t_hi_by_stratum["model|same"] is None
    assert row.t_hi_by_stratum["K-C|same"] is None
    assert row.store_floor == 0.02 and row.t_hi == 1.0
    assert 0.18 < row.t_lo < 0.19
    with pytest.raises(ValueError, match="unknown settings keys"):
        Settings.from_json(score_lane.repo_path("w5_strata", score_lane.SETTINGS_DIR))
