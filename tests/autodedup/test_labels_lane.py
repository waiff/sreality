"""The `labels` lane against a fake connection — no database, no network.

What this file pins is the contract the fits downstream depend on: one row per pair, explicit
beating implied, the member-pair expansion only for groups the operator CONFIRMED, the cap on
group size, the engine view carried as a snapshot (absent when the engine never stored the
pair), and the fact that the whole mode is read-only.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from autodedup import labels_lane
from autodedup import lane as lane_module
from autodedup.labels_sql import (
    CLUSTER_MEMBERS_SQL,
    CLUSTER_VERDICTS_SQL,
    ENGINE_PAIRS_SQL,
    GENERATION_CLUSTERS_SQL,
    MUST_NOT_LINK_SQL,
    PAIR_VERDICTS_SQL,
    STORE_PRESENT_SQL,
)

DECIDED = datetime(2026, 9, 18, 10, 30, tzinfo=timezone.utc)

COLUMNS: dict[int, tuple[str, ...]] = {
    id(STORE_PRESENT_SQL): ("present",),
    id(GENERATION_CLUSTERS_SQL): ("n_clusters", "max_size"),
    id(PAIR_VERDICTS_SQL): (
        "listing_lo", "listing_hi", "verdict", "note", "reasons", "decided_by", "decided_at",
    ),
    id(CLUSTER_VERDICTS_SQL): (
        "cluster_key", "verdict", "note", "reasons", "decided_by", "decided_at", "size",
        "generation",
    ),
    id(CLUSTER_MEMBERS_SQL): ("cluster_key", "listing_id"),
    id(ENGINE_PAIRS_SQL): (
        "listing_lo", "listing_hi", "score", "zone", "decision", "guard_veto", "cluster_key",
        "model_version", "feature_version", "families", "decided_at",
    ),
    id(MUST_NOT_LINK_SQL): ("listing_lo", "listing_hi", "source", "reason", "created_at"),
}


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.description: list[tuple[str]] | None = None

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((sql, params))
        if sql.lstrip().upper().startswith("SET LOCAL"):
            return
        names = COLUMNS[id(sql)]
        self.description = [(name,) for name in names]
        self._rows = list(self._conn.rows.get(id(sql), ()))
        if sql is ENGINE_PAIRS_SQL and params:
            wanted = set(zip(params["los"], params["his"]))
            self._rows = [row for row in self._rows if (row[0], row[1]) in wanted]
        if sql is CLUSTER_MEMBERS_SQL and params:
            keys = set(params["keys"])
            self._rows = [row for row in self._rows if row[0] in keys]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class FakeConn:
    def __init__(self, executed: list[tuple[str, Any]], rows: dict[int, Any]) -> None:
        self.executed = executed
        self.rows = rows
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def close(self) -> None:
        self.closed = True


def _store() -> dict[int, Any]:
    rows: dict[int, Any] = {
        id(STORE_PRESENT_SQL): [(True,)],
        id(GENERATION_CLUSTERS_SQL): [(3, 4)],
        # 11/12: ruled same by hand. 21/22: ruled different. 31/32: inside a confirmed group
        # but separated explicitly — the case `explicit wins` exists for. 41/42: unsure.
        id(PAIR_VERDICTS_SQL): [
            (11, 12, "same", "stejny byt", ["same_floor_plan"], "operator", DECIDED),
            (21, 22, "different", None, [], "operator", DECIDED),
            (31, 32, "same_project_different_unit", "jiny dum", [], "operator", DECIDED),
            (41, 42, "unsure", None, [], "operator", DECIDED),
        ],
        id(CLUSTER_VERDICTS_SQL): [
            (900, "same", "potvrzeno", ["same_photos"], "operator", DECIDED, 3, "g4"),
            (901, "different", None, [], "operator", DECIDED, 2, "g4"),
            (902, "same", None, [], "operator", DECIDED, 3, "g4"),
        ],
        id(CLUSTER_MEMBERS_SQL): [
            (900, 31), (900, 32), (900, 33),
            (901, 51), (901, 52),
            (902, 61), (902, 62), (902, 63),
        ],
        id(ENGINE_PAIRS_SQL): [
            (11, 12, 0.93, "band", "model", None, None, "w5_gold", 4, 36, DECIDED),
            (31, 32, 0.81, "band", "model", None, 900, "w5_gold", 4, 36, DECIDED),
        ],
        id(MUST_NOT_LINK_SQL): [(21, 22, "operator", "different unit", DECIDED)],
    }
    return rows


@pytest.fixture()
def lane():
    """`lane(out, **args) -> summary`, with `.executed` and `.conns` attached."""
    executed: list[tuple[str, Any]] = []
    conns: list[FakeConn] = []
    rows = _store()

    def factory() -> FakeConn:
        conn = FakeConn(executed, rows)
        conns.append(conn)
        return conn

    def run(out: Path, **args: Any) -> dict[str, Any]:
        raw = {key: str(value) for key, value in args.items()}
        return labels_lane.run_labels(factory, raw, Path(out))

    run.executed = executed  # type: ignore[attr-defined]
    run.conns = conns  # type: ignore[attr-defined]
    run.rows = rows  # type: ignore[attr-defined]
    return run


def _read(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --- arguments -----------------------------------------------------------------------------


def test_unknown_arg_is_fatal() -> None:
    with pytest.raises(SystemExit):
        labels_lane.parse_args({"generaton": "g4"})


@pytest.mark.parametrize("raw", [{"generation": "G4"}, {"generation": "4g"},
                                 {"generation": "g4; drop table"}, {"generation": "../x"}])
def test_generation_must_be_a_slug(raw: dict[str, str]) -> None:
    with pytest.raises(SystemExit):
        labels_lane.parse_args(raw)


@pytest.mark.parametrize("raw", [{"max_members": "1"}, {"max_members": "999"},
                                 {"max_members": "-2"}, {"max_members": "many"}])
def test_max_members_is_bounded(raw: dict[str, str]) -> None:
    with pytest.raises(SystemExit):
        labels_lane.parse_args(raw)


def test_defaults() -> None:
    parsed = labels_lane.parse_args({})
    assert parsed.generation == labels_lane.DEFAULT_GENERATION
    assert parsed.max_members == labels_lane.DEFAULT_MAX_MEMBERS


# --- the artifact --------------------------------------------------------------------------


def test_explicit_and_implied_labels_land_in_one_sorted_file(lane, tmp_path: Path) -> None:
    summary = lane(tmp_path, generation="g4")
    records = _read(tmp_path / labels_lane.LABELS_FILE)

    keys = [(row["listing_lo"], row["listing_hi"]) for row in records]
    assert keys == sorted(keys)
    assert (11, 12) in keys and (41, 42) in keys
    # cluster 900 (confirmed, three members) and 902 (confirmed) expand; 901 does not.
    assert (31, 33) in keys and (32, 33) in keys
    assert (61, 62) in keys and (61, 63) in keys and (62, 63) in keys
    assert (51, 52) not in keys
    assert summary["counts"]["explicit"] == 4
    assert summary["counts"]["cluster_clusters_same"] == 2


def test_an_explicit_verdict_beats_the_group_that_implies_the_pair(lane, tmp_path: Path) -> None:
    lane(tmp_path)
    records = {(r["listing_lo"], r["listing_hi"]): r for r in
               _read(tmp_path / labels_lane.LABELS_FILE)}
    ruled = records[(31, 32)]
    assert ruled["source"] == labels_lane.SOURCE_EXPLICIT
    assert ruled["verdict"] == "same_project_different_unit"
    assert records[(31, 33)]["source"] == labels_lane.SOURCE_IMPLIED
    assert records[(31, 33)]["cluster_key"] == 900


def test_notes_and_reasons_are_carried_verbatim(lane, tmp_path: Path) -> None:
    lane(tmp_path)
    records = {(r["listing_lo"], r["listing_hi"]): r for r in
               _read(tmp_path / labels_lane.LABELS_FILE)}
    assert records[(11, 12)]["note"] == "stejny byt"
    assert records[(11, 12)]["reasons"] == ["same_floor_plan"]
    assert records[(31, 33)]["note"] == "potvrzeno"


def test_the_relation_is_the_judge_vocabulary(lane, tmp_path: Path) -> None:
    lane(tmp_path)
    records = {(r["listing_lo"], r["listing_hi"]): r for r in
               _read(tmp_path / labels_lane.LABELS_FILE)}
    assert records[(11, 12)]["relation"] == "same_property"
    assert records[(21, 22)]["relation"] == "different_property"
    assert records[(41, 42)]["relation"] == "insufficient_evidence"
    assert records[(31, 32)]["relation"] == "same_project_different_unit"


def test_the_engine_view_is_a_snapshot_and_absent_when_unstored(lane, tmp_path: Path) -> None:
    summary = lane(tmp_path)
    records = {(r["listing_lo"], r["listing_hi"]): r for r in
               _read(tmp_path / labels_lane.LABELS_FILE)}
    assert records[(11, 12)]["engine"] == {
        "score": pytest.approx(0.93), "zone": "band", "decision": "model", "guard_veto": None,
        "cluster_key": None, "model_version": "w5_gold", "feature_version": 4, "families": 36,
        "scored_at": DECIDED.isoformat(),
    }
    assert records[(21, 22)]["engine"] is None
    assert summary["by_zone"] == {"band": 2, "unstored": len(records) - 2}


def test_must_not_link_is_its_own_artifact_and_a_flag_on_the_label(lane, tmp_path: Path) -> None:
    summary = lane(tmp_path)
    mnl = _read(tmp_path / labels_lane.MUST_NOT_LINK_FILE)
    assert mnl == [{"listing_lo": 21, "listing_hi": 22, "source": "operator",
                    "reason": "different unit", "created_at": DECIDED.isoformat()}]
    records = {(r["listing_lo"], r["listing_hi"]): r for r in
               _read(tmp_path / labels_lane.LABELS_FILE)}
    assert records[(21, 22)]["must_not_link"] is True
    assert records[(11, 12)]["must_not_link"] is False
    assert summary["counts"]["must_not_link"] == 1


def test_a_group_over_the_cap_is_skipped_and_counted(lane, tmp_path: Path) -> None:
    summary = lane(tmp_path, max_members=2)
    records = _read(tmp_path / labels_lane.LABELS_FILE)
    assert summary["counts"]["cluster_clusters_over_cap"] == 2
    assert all(row["source"] == labels_lane.SOURCE_EXPLICIT for row in records)


def test_two_runs_over_an_unchanged_store_are_byte_identical(lane, tmp_path: Path) -> None:
    lane(tmp_path / "a")
    lane(tmp_path / "b")
    for name in (labels_lane.LABELS_FILE, labels_lane.MUST_NOT_LINK_FILE):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_the_mode_never_writes(lane, tmp_path: Path) -> None:
    lane(tmp_path)
    for sql, _params in lane.executed:
        head = sql.strip().split()[0].upper()
        assert head in {"SELECT", "SET"}, sql
    assert all(conn.closed for conn in lane.conns)


def test_an_absent_store_is_fatal(lane, tmp_path: Path) -> None:
    lane.rows[id(STORE_PRESENT_SQL)] = [(False,)]
    with pytest.raises(SystemExit):
        lane(tmp_path)


def test_the_mode_is_registered_with_its_ledger_meta() -> None:
    assert lane_module.MODES["labels"] is labels_lane.run_labels
    meta = lane_module.ITERATION_META["labels"]
    assert meta["wave"] == "W6" and meta["title"] and meta["approach"]
    assert "autodedup.labels_lane" in meta["tools"]


def test_the_workflow_documents_the_mode() -> None:
    body = (Path(__file__).resolve().parents[2] / ".github" / "workflows"
            / "autodedup.yml").read_text(encoding="utf-8")
    assert "#   labels" in body
    flattened = " ".join(body.split())
    assert "labels accepts generation" in flattened
    for key in labels_lane.ARG_KEYS:
        assert key in flattened


# --- the harness flags ---------------------------------------------------------------------


def _artifact(path: Path) -> Path:
    path.write_text(
        "\n".join(json.dumps(row) for row in [
            {"listing_lo": 11, "listing_hi": 12, "verdict": "same",
             "source": labels_lane.SOURCE_EXPLICIT},
            {"listing_lo": 21, "listing_hi": 22, "verdict": "same",
             "source": labels_lane.SOURCE_IMPLIED, "cluster_key": 900},
            {"listing_lo": 31, "listing_hi": 32, "verdict": "unsure",
             "source": labels_lane.SOURCE_EXPLICIT},
        ]) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("command", ["fit", "evaluate", "errors"])
def test_every_labelled_command_takes_the_operator_flags(command: str, tmp_path: Path) -> None:
    from autodedup import harness

    path = _artifact(tmp_path / "operator_labels.jsonl")
    args = harness.build_parser().parse_args([
        command, str(tmp_path), "--judgements", "j.jsonl", "--out", str(tmp_path),
        "--operator-labels", str(path),
    ])
    tier = harness.operator_tier(args)
    assert set(tier) == {(11, 12), (21, 22)}  # `unsure` is not a label
    assert tier[(11, 12)].tier == "operator" and tier[(11, 12)].y == 1


def test_the_flags_exclude_or_discount_the_implied_labels(tmp_path: Path) -> None:
    from autodedup import harness

    path = _artifact(tmp_path / "operator_labels.jsonl")
    base = ["fit", str(tmp_path), "--judgements", "j.jsonl", "--out", str(tmp_path),
            "--operator-labels", str(path)]
    parser = harness.build_parser()
    assert set(harness.operator_tier(parser.parse_args(base + ["--exclude-implied"]))) == {
        (11, 12)
    }
    weighted = harness.operator_tier(parser.parse_args(base + ["--implied-weight", "0.2"]))
    assert weighted[(21, 22)].weight == pytest.approx(0.2)
    assert weighted[(11, 12)].weight == pytest.approx(1.0)


def test_no_operator_flag_means_no_operator_tier(tmp_path: Path) -> None:
    from autodedup import harness

    args = harness.build_parser().parse_args([
        "fit", str(tmp_path), "--judgements", "j.jsonl", "--out", str(tmp_path),
    ])
    assert harness.operator_tier(args) == {}


def test_a_missing_operator_labels_file_is_fatal(tmp_path: Path) -> None:
    from autodedup import harness

    args = harness.build_parser().parse_args([
        "fit", str(tmp_path), "--judgements", "j.jsonl", "--out", str(tmp_path),
        "--operator-labels", str(tmp_path / "nope.jsonl"),
    ])
    with pytest.raises(SystemExit):
        harness.operator_tier(args)
