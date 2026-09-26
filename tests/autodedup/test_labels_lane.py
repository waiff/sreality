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
    OPERATOR_MERGE_LINKS_SQL,
    OPERATOR_MERGE_MEMBERS_SQL,
    OPERATOR_MERGES_PRESENT_SQL,
    OPERATOR_MERGES_SQL,
    PAIR_VERDICTS_SQL,
    SAMPLE_RANK_SQL,
    STORE_PRESENT_SQL,
)

DECIDED = datetime(2026, 9, 18, 10, 30, tzinfo=timezone.utc)

COLUMNS: dict[int, tuple[str, ...]] = {
    id(STORE_PRESENT_SQL): ("present",),
    id(GENERATION_CLUSTERS_SQL): ("n_clusters", "max_size"),
    id(PAIR_VERDICTS_SQL): (
        "listing_lo", "listing_hi", "verdict", "note", "reasons", "decided_by", "decided_at",
        "verdict_id",
    ),
    id(CLUSTER_VERDICTS_SQL): (
        "cluster_key", "verdict", "note", "reasons", "decided_by", "decided_at",
        "generation", "member_ids", "size",
    ),
    id(CLUSTER_MEMBERS_SQL): ("cluster_key", "listing_id"),
    id(ENGINE_PAIRS_SQL): (
        "listing_lo", "listing_hi", "score", "zone", "decision", "guard_veto", "cluster_key",
        "model_version", "feature_version", "families", "decided_at",
    ),
    id(MUST_NOT_LINK_SQL): ("listing_lo", "listing_hi", "source", "reason", "created_at"),
    id(SAMPLE_RANK_SQL): ("cluster_key", "sample_rank"),
    id(OPERATOR_MERGES_PRESENT_SQL): ("present",),
    id(OPERATOR_MERGES_SQL): (
        "merge_group_id", "merged_at", "survivor_property_id", "retired_property_ids",
        "member_ids", "member_sides", "member_property_ids", "n_pairs", "decided_by", "source",
        "status", "status_note", "status_at", "copied_at",
    ),
    id(OPERATOR_MERGE_LINKS_SQL): ("verdict_id", "operator_merge_group_id"),
    id(OPERATOR_MERGE_MEMBERS_SQL): (
        "listing_id", "source", "category_type", "category_main", "property_id", "obec_kod",
        "cast_obce_kod",
    ),
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
        if sql in (CLUSTER_MEMBERS_SQL, SAMPLE_RANK_SQL) and params:
            keys = set(params["keys"])
            self._rows = [row for row in self._rows if row[0] in keys]
        if sql is OPERATOR_MERGE_MEMBERS_SQL and params:
            ids = set(params["ids"])
            self._rows = [row for row in self._rows if row[0] in ids]

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
            (11, 12, "same", "stejny byt", ["same_floor_plan"], "operator", DECIDED, 1),
            (21, 22, "different", None, [], "operator", DECIDED, 2),
            (31, 32, "same_project_different_unit", "jiny dum", [], "operator", DECIDED, 3),
            (41, 42, "unsure", None, [], "operator", DECIDED, 4),
        ],
        # `member_ids` is the set the operator ruled on (E58) — the implied labels come off
        # THIS, not off `cluster_members`, which is read only for the run summary.
        id(CLUSTER_VERDICTS_SQL): [
            (900, "same", "potvrzeno", ["same_photos"], "operator", DECIDED,
             "g4", [31, 32, 33], 3),
            (901, "different", None, [], "operator", DECIDED, "g4", [51, 52], 2),
            (902, "same", None, [], "operator", DECIDED, "g4", [61, 62, 63], 3),
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
        # The seeded order the UI served: 902 came up before 900, 901 last.
        id(SAMPLE_RANK_SQL): [(902, 1), (900, 2), (901, 3)],
        # Migration 564 not applied: the default store is the pre-E299 one.
        id(OPERATOR_MERGES_PRESENT_SQL): [(False,)],
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
    # The canned store, so a test can say what the CURRENT clustering looks like.
    run.rows = rows  # type: ignore[attr-defined]
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
        # WITH is a reading head too — the engine view leads with a CTE that scopes the pass
        # to one generation — but only when no writing statement follows it.
        assert head in {"SELECT", "SET", "WITH"}, sql
        assert not {"INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE"} & {
            token.strip("(,;").upper() for token in sql.split()
        }, sql
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


def test_the_decider_leaves_as_a_stable_digest_not_an_email(lane, tmp_path: Path) -> None:
    lane(tmp_path)
    records = _read(tmp_path / labels_lane.LABELS_FILE)
    deciders = {row["decided_by"] for row in records}
    assert deciders == {labels_lane.decider("operator")}
    assert all(value.startswith(labels_lane.DECIDED_BY_PREFIX) for value in deciders)
    # The artifact is uploaded by a workflow in a public repository: no login may reach it.
    body = (tmp_path / labels_lane.LABELS_FILE).read_text(encoding="utf-8")
    assert "@" not in body
    assert labels_lane.decider("a@b.cz") != labels_lane.decider("c@d.cz")
    assert labels_lane.decider("a@b.cz") == labels_lane.decider(" a@b.cz ")
    assert labels_lane.decider(None) is None


def test_the_engine_view_is_scoped_to_the_generation(lane, tmp_path: Path) -> None:
    """`autodedup.pairs` accumulates every pass ever scored.

    W6 found 153 of 444 explicit labels whose `engine.zone` came from g2 or g3 rows because the
    read was unscoped — which is the difference between "this label sits in the band g4 pays a
    judge for" and "some older model once banded it"."""
    lane(tmp_path, generation="g3")
    engine_calls = [
        params for sql, params in lane.executed if sql is ENGINE_PAIRS_SQL
    ]
    assert engine_calls, "the engine view was never read"
    assert all(params.get("generation") == "g3" for params in engine_calls)
    # Since migration 538 the scope is the pair's OWN column — the derivation through the
    # clusters' (model_version, feature_version) broke the moment a generation's clusters were
    # re-stamped away, which is exactly what happened to g4.
    assert "p.generation = %(generation)s::text" in ENGINE_PAIRS_SQL
    assert "autodedup.clusters" not in ENGINE_PAIRS_SQL


@pytest.mark.parametrize("command", ["fit", "evaluate", "errors"])
def test_the_operator_tier_alone_is_a_usable_label_source(command: str, tmp_path: Path) -> None:
    """The operator outranks gold, so requiring a judgements file to measure against the
    operator's own testimony made the top tier unusable on its own."""
    from autodedup import harness

    path = _artifact(tmp_path / "operator_labels.jsonl")
    args = harness.build_parser().parse_args([
        command, str(tmp_path), "--out", str(tmp_path), "--operator-labels", str(path),
    ])
    assert harness.judgement_paths(args) == []
    assert set(harness.operator_tier(args)) == {(11, 12), (21, 22)}


@pytest.mark.parametrize("command", ["fit", "evaluate", "errors"])
def test_naming_no_label_source_at_all_is_refused(command: str, tmp_path: Path) -> None:
    from autodedup import harness

    args = harness.build_parser().parse_args([
        command, str(tmp_path), "--out", str(tmp_path),
    ])
    assert harness.judgement_paths(args) is None


def test_a_missing_judgements_file_is_still_refused(tmp_path: Path) -> None:
    from autodedup import harness

    args = harness.build_parser().parse_args([
        "fit", str(tmp_path), "--out", str(tmp_path),
        "--judgements", str(tmp_path / "nope.jsonl"),
    ])
    assert harness.judgement_paths(args) is None


# --- the seeded sample rank ------------------------------------------------------------------


@pytest.mark.parametrize("raw", [{"sample_seed": "V1"}, {"sample_seed": "v-1"},
                                 {"sample_seed": "seventeencharacter"},
                                 {"sample_seed": "v1' or 1=1"}])
def test_the_sample_seed_is_a_closed_charset(raw: dict[str, str]) -> None:
    with pytest.raises(SystemExit):
        labels_lane.parse_args(raw)


def test_the_sample_seed_defaults_to_the_session_the_operator_ran(lane, tmp_path: Path) -> None:
    summary = lane(tmp_path)
    assert labels_lane.parse_args({}).sample_seed == labels_lane.DEFAULT_SAMPLE_SEED == "v1"
    assert summary["params"]["sample_seed"] == "v1"
    # The seed NAMES the sample: a rank without it is a number nobody can reproduce.
    assert summary["generation"]["sample_seed"] == "v1"
    calls = [params for sql, params in lane.executed if sql is SAMPLE_RANK_SQL]
    assert calls and all(params["seed"] == "v1" for params in calls)
    assert all(params["generation"] == "g4" for params in calls)


def test_an_implied_label_carries_its_group_s_place_in_the_sample(lane, tmp_path: Path) -> None:
    """The unbiased sample (100 of 100 confirmed) is "the first N groups of seed v1", and it
    was reproducible only from the live store until the rank travelled in the artifact."""
    lane(tmp_path)
    records = {(r["listing_lo"], r["listing_hi"]): r for r in
               _read(tmp_path / labels_lane.LABELS_FILE)}
    assert records[(31, 33)]["cluster_key"] == 900 and records[(31, 33)]["sample_rank"] == 2
    assert records[(61, 62)]["cluster_key"] == 902 and records[(61, 62)]["sample_rank"] == 1
    # An explicit verdict was typed against a pair, not drawn from the group queue.
    assert records[(11, 12)]["sample_rank"] is None
    assert records[(31, 32)]["sample_rank"] is None


def test_the_rank_is_over_the_whole_generation_not_the_labelled_subset() -> None:
    """A rank counted over confirmed groups only would renumber itself with every new ruling —
    so the window runs over `autodedup.clusters` for the generation and is filtered after."""
    body = " ".join(SAMPLE_RANK_SQL.split())
    assert "row_number() over ( order by md5(c.cluster_key::text || %(seed)s::text) asc," in body
    assert "from autodedup.clusters c where c.generation = %(generation)s::text" in body
    assert "where r.cluster_key = any(%(keys)s::bigint[])" in body


def test_the_rank_reaches_the_label_loader(lane, tmp_path: Path) -> None:
    from autodedup import labels as labels_module

    lane(tmp_path)
    rows = {row.key: row for row in
            labels_module.load_operator_labels(tmp_path / labels_lane.LABELS_FILE)}
    assert rows[(31, 33)].sample_rank == 2
    assert rows[(11, 12)].sample_rank is None


# ------------------------------- E58: the implied labels come off the verdict's own member set


def test_implied_labels_come_from_the_verdict_not_from_todays_clustering(lane, tmp_path: Path):
    """The set the operator was looking at is recorded ON THE RULING (migration 538).

    Before it was, this lane expanded a confirmation over `cluster_members` read at export
    time — so re-clustering a key silently changed what an old label asserted, and erasing the
    generation's clusters (which promoting g5 did to g4) made the export impossible to
    reproduce at all. Here the current clustering says something else entirely and the labels
    ignore it.
    """
    # The store now clusters 900 as {31, 32, 99} — a bridge pulled 99 in and dropped 33.
    lane.rows[id(CLUSTER_MEMBERS_SQL)] = [(900, 31), (900, 32), (900, 99)]
    lane(tmp_path)
    implied = {
        (row["listing_lo"], row["listing_hi"])
        for row in _read(tmp_path / labels_lane.LABELS_FILE)
        if row["source"] == "implied" and row["cluster_key"] == 900
    }
    # The ruled set was {31, 32, 33}; 31-32 is shadowed by an explicit verdict.
    assert implied == {(31, 33), (32, 33)}
    assert not any(99 in pair for pair in implied)


def test_a_legacy_ruling_falls_back_to_its_own_generations_members() -> None:
    """A row taken before migration 538 carries no set; the statement resolves it to the
    members of the generation being exported, which is the most that can honestly be said."""
    flat = " ".join(CLUSTER_VERDICTS_SQL.split())
    assert "coalesce(v.member_ids, mem.ids) as member_ids" in flat
    assert "m.generation = %(generation)s::text" in flat
    assert "or (v.generation is null and mem.ids is not null)" in flat


def test_a_ruling_applies_to_a_generation_whose_group_it_names() -> None:
    """The labels lane answers "does this ruling apply here?" the way the Groups page does —
    on the SET (E58). After migration 538 every backfilled ruling reads `generation = 'g4'`,
    so a generation-STRING test alone would export zero implied labels for g5 while the UI
    shows 203 of those same rulings applying to g5 groups that never moved.
    """
    flat = " ".join(CLUSTER_VERDICTS_SQL.split())
    assert "or (v.member_ids is not null and v.member_ids = mem.ids)" in flat
    # The string arm survives as a UNION, not as the test: it is what keeps a pass's own
    # rulings exportable while that pass's clusters are missing from the store.
    assert "v.generation = %(generation)s::text or (v.member_ids is not null" in flat


# ------------------------------------ E299: the operator's Browse merges (migration 564)

MERGED = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
COPIED = datetime(2026, 9, 25, 22, 0, tzinfo=timezone.utc)


def _with_merges(lane: Any) -> None:
    """Three Browse merges, as 564 copied them.

    g-1 united 71 (from property 100) with 72 and 73 (both from 200); 74 came from 300 but
    was detached since (it sits on 501), so it joins no pair. 71-72 still stands on the merge's
    own ruling; 71-73 the operator separated by hand afterwards. g-2's one pair was never
    ruled at all; g-3's pair carries a permanent negative and no ruling."""
    rows = lane.rows
    rows[id(OPERATOR_MERGES_PRESENT_SQL)] = [(True,)]
    rows[id(PAIR_VERDICTS_SQL)] = rows[id(PAIR_VERDICTS_SQL)] + [
        (71, 72, "same", "operator merge g-1 (copied by migration 560)", [], "operator",
         MERGED, 5),
        (71, 73, "different", "jiny byt", [], "op@example.cz", DECIDED, 6),
    ]
    rows[id(OPERATOR_MERGE_LINKS_SQL)] = [(5, "g-1")]
    rows[id(MUST_NOT_LINK_SQL)] = rows[id(MUST_NOT_LINK_SQL)] + [
        (91, 92, "operator", "jiny dum", DECIDED),
    ]
    rows[id(OPERATOR_MERGES_SQL)] = [
        ("g-1", MERGED, 500, [200, 300], [71, 72, 73, 74], [100, 200, 200, 300],
         [500, 500, 500, 501], 2, "operator", "browse", "live", None, None, COPIED),
        ("g-2", MERGED.replace(day=2), 600, [601], [81, 82], [600, 601], [600, 600], 1,
         "operator", "browse", "live", None, None, COPIED),
        ("g-3", MERGED.replace(day=3), 700, [701], [91, 92], [700, 701], [700, 700], 1,
         "operator", "browse", "undone", "operator unmerged", DECIDED, COPIED),
    ]
    rows[id(OPERATOR_MERGE_MEMBERS_SQL)] = [
        (71, "sreality", "prodej", "byt", 500, 554782, 490245),
        (72, "idnes", "prodej", "byt", 500, 554782, 490245),
        (73, "bazos", "prodej", "byt", 500, 563510, 5001),
        (81, "remax", "pronajem", "byt", 600, 577626, None),
        (82, "idnes", "pronajem", "byt", 600, 577626, None),
    ]


def _merges(path: Path) -> dict[str, dict[str, Any]]:
    return {row["merge_group_id"]: row for row in _read(path / labels_lane.OPERATOR_MERGES_FILE)}


def test_a_browse_merge_ruling_leaves_as_browse_merge_with_its_group(lane, tmp_path: Path):
    _with_merges(lane)
    summary = lane(tmp_path)
    records = {(r["listing_lo"], r["listing_hi"]): r for r in
               _read(tmp_path / labels_lane.LABELS_FILE)}
    assert records[(71, 72)]["source"] == "browse_merge"
    assert records[(71, 72)]["merge_group_id"] == "g-1"
    # Separated by hand after the merge: the operator's later word, and it is explicit.
    assert records[(71, 73)]["source"] == labels_lane.SOURCE_EXPLICIT
    assert records[(71, 73)]["merge_group_id"] is None
    # Append-only: every row carries the new field, the old ones unchanged.
    assert all("merge_group_id" in row for row in records.values())
    assert records[(11, 12)]["merge_group_id"] is None
    assert summary["counts"]["browse_merge"] == 1
    assert summary["counts"]["explicit"] == 5
    assert summary["format"] == {"operator_labels": labels_lane.LABELS_FORMAT,
                                 "operator_merges": 1}


def test_the_group_file_carries_members_sides_places_and_pair_standings(lane, tmp_path: Path):
    _with_merges(lane)
    lane(tmp_path)
    groups = _merges(tmp_path)
    assert list(groups) == ["g-1", "g-2", "g-3"]  # merged_at order
    g1 = groups["g-1"]
    assert g1["provenance"] == "browse_merge" and g1["source"] == "browse"
    assert g1["status"] == "live" and g1["format"] == 1
    assert [m["listing_id"] for m in g1["members"]] == [71, 72, 73, 74]
    assert [m["side"] for m in g1["members"]] == [100, 200, 200, 300]
    members = {m["listing_id"]: m for m in g1["members"]}
    assert members[71]["block"] == "quarter:490245"  # Praha: its quarter
    assert members[73]["block"] == "town:563510"     # elsewhere: its town
    assert members[74]["block"] is None and members[74]["property_id"] is None
    assert members[71]["source"] == "sreality" and members[71]["property_id_at_copy"] == 500
    assert g1["pairs"] == [
        {"listing_lo": 71, "listing_hi": 72, "standing": "browse_merge", "verdict": "same",
         "must_not_link": False},
        {"listing_lo": 71, "listing_hi": 73, "standing": "explicit", "verdict": "different",
         "must_not_link": False},
    ]
    assert (g1["n_pairs"], g1["n_pairs_at_copy"], g1["n_pairs_same"]) == (2, 2, 1)
    assert g1["blocks"] == ["quarter:490245", "town:563510"]
    assert g1["one_property_now"] is False
    assert groups["g-2"]["pairs"][0]["standing"] == "unruled"
    assert groups["g-2"]["n_pairs_same"] == 1 and groups["g-2"]["one_property_now"] is True
    g3 = groups["g-3"]
    assert g3["pairs"][0]["standing"] == "must_not_link" and g3["n_pairs_same"] == 0
    assert g3["status"] == "undone" and g3["status_note"] == "operator unmerged"
    assert g1["decided_by"] == labels_lane.decider("operator")


def test_the_group_file_round_trips_through_the_label_store(lane, tmp_path: Path):
    from autodedup.labels import load_operator_merges

    _with_merges(lane)
    lane(tmp_path)
    merges = {m.merge_group_id: m for m in
              load_operator_merges(tmp_path / labels_lane.OPERATOR_MERGES_FILE)}
    assert [p.key for p in merges["g-1"].ruled_pairs] == [(71, 72)]
    assert [p.key for p in merges["g-2"].ruled_pairs] == [(81, 82)]
    assert merges["g-3"].ruled_pairs == [] and merges["g-3"].status == "undone"


def test_the_summary_ranks_the_blocks_an_export_would_need(lane, tmp_path: Path):
    _with_merges(lane)
    summary = lane(tmp_path)
    merges = summary["operator_merges"]
    assert merges["present"] is True and merges["groups"] == 3
    assert merges["pairs"] == 4 and merges["pairs_same"] == 2
    assert merges["pairs_by_standing"] == {"browse_merge": 1, "explicit": 1,
                                           "must_not_link": 1, "unruled": 1}
    assert merges["by_status"] == {"live": 2, "undone": 1}
    assert merges["linked_rulings"] == 1
    assert merges["blocks_ranked"][0] == {"block": "quarter:490245", "groups": 1}
    assert {row["block"] for row in merges["blocks_ranked"]} == {
        "quarter:490245", "town:563510", "town:577626"}
    assert summary["artifacts"]["operator_merges"].endswith(labels_lane.OPERATOR_MERGES_FILE)


def test_without_migration_564_the_group_file_is_empty_and_says_why(lane, tmp_path: Path):
    summary = lane(tmp_path)
    assert (tmp_path / labels_lane.OPERATOR_MERGES_FILE).read_text(encoding="utf-8") == ""
    assert summary["operator_merges"]["present"] is False
    assert summary["counts"]["browse_merge"] == 0
    executed = {id(sql) for sql, _params in lane.executed}
    assert id(OPERATOR_MERGES_SQL) not in executed
    assert id(OPERATOR_MERGE_LINKS_SQL) not in executed


def test_the_merge_reads_are_selects_and_never_touch_the_legacy_ledger(lane, tmp_path: Path):
    _with_merges(lane)
    lane(tmp_path / "a")
    lane(tmp_path / "b")
    for sql, _params in lane.executed:
        assert sql.strip().split()[0].upper() in {"SELECT", "SET", "WITH"}, sql
    for sql in (OPERATOR_MERGES_PRESENT_SQL, OPERATOR_MERGES_SQL, OPERATOR_MERGE_LINKS_SQL,
                OPERATOR_MERGE_MEMBERS_SQL, PAIR_VERDICTS_SQL):
        assert "property_merge_events" not in sql
    assert "autodedup.operator_merges" in OPERATOR_MERGES_SQL
    for name in (labels_lane.LABELS_FILE, labels_lane.OPERATOR_MERGES_FILE):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
    assert "@" not in (tmp_path / "a" / labels_lane.OPERATOR_MERGES_FILE).read_text("utf-8")


def test_the_browse_merge_flags_reach_the_operator_tier(tmp_path: Path) -> None:
    from autodedup import harness

    path = tmp_path / "operator_labels.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in [
        {"listing_lo": 11, "listing_hi": 12, "verdict": "same", "source": "explicit"},
        {"listing_lo": 71, "listing_hi": 72, "verdict": "same", "source": "browse_merge",
         "merge_group_id": "g-1"},
    ]) + "\n", encoding="utf-8")
    base = ["fit", str(tmp_path), "--out", str(tmp_path), "--operator-labels", str(path)]
    parser = harness.build_parser()
    tier = harness.operator_tier(parser.parse_args(base))
    assert set(tier) == {(11, 12), (71, 72)} and tier[(71, 72)].source == "browse_merge"
    assert set(harness.operator_tier(parser.parse_args(base + ["--exclude-browse-merge"]))) \
        == {(11, 12)}
    weighted = harness.operator_tier(parser.parse_args(base + ["--browse-merge-weight", "0.5"]))
    assert weighted[(71, 72)].weight == pytest.approx(0.5)
    assert weighted[(11, 12)].weight == pytest.approx(1.0)
