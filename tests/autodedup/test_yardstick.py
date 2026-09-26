"""`harness yardstick` (E299) over the engine's own synthetic cohort — no database, no network.

The cohort is the end-to-end fixture's, so every outcome the yardstick names is produced by the
real pipeline rather than asserted into a canned row: a cross-portal duplicate the engine groups
(`together`), a sale and a rental of one flat the rule floor refuses before any probe
(`vetoed_at_blocking`), the developer tie the band holds (`band`), a filler pair that never
shares a key (`never_paired`), a stored reject, a reject scored below the store floor, and a
merge a must-not-link keeps apart (`split_by_invariant`). The operator groups arrive in every
accepted shape: the labels lane's group file, a dump of `autodedup.operator_merges`, and the
`browse_merge` rows of `operator_labels.jsonl`.
"""

from __future__ import annotations

import gzip
import io
import json
from pathlib import Path
from typing import Any

import pytest

from autodedup import harness
from autodedup import yardstick as Y
from autodedup.labels import (
    STANDING_BROWSE_MERGE,
    STANDING_EXPLICIT,
    load_operator_merges,
    merge_pairs,
    parse_operator_merge,
)
from tests.autodedup.test_engine_e2e import (
    DUP_A,
    DUP_B,
    PROJECT_TIE,
    RENT,
    SALE,
    build_records,
)

FILLER_REJECT = (3005, 3007)
NEVER = (2001, 3003)
ABSENT = 9_999_999


@pytest.fixture(scope="module")
def cohort(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("yard") / "cohort.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for record in build_records():
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def _run(cohort: Path, out: Path, *extra: str) -> Path:
    assert harness.main(["run", str(cohort), "--out", str(out), *extra], out=io.StringIO()) == 0
    return out


@pytest.fixture(scope="module")
def run_dir(cohort: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _run(cohort, tmp_path_factory.mktemp("run") / "r")


def _dump(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def _table_row(group: str, members: list[int], sides: list[int], **over: Any) -> dict[str, Any]:
    row = {"merge_group_id": group, "member_ids": members, "member_sides": sides,
           "member_property_ids": [7] * len(members), "status": "live"}
    row.update(over)
    return row


def _groups(tmp_path: Path) -> Path:
    return _dump(tmp_path / "operator_merges.json", [
        _table_row("g-dup", [DUP_A, DUP_B], [1, 2]),
        _table_row("g-sale-rent", [SALE, RENT], [3, 4]),
        _table_row("g-tie", list(PROJECT_TIE), [5, 6]),
        _table_row("g-reject", list(FILLER_REJECT), [8, 9]),
        _table_row("g-never", list(NEVER), [10, 11]),
        _table_row("g-absent", [DUP_A, ABSENT], [12, 13]),
    ])


def _yardstick(cohort: Path, run: Path, groups: Path, out: Path, *extra: str) -> dict[str, Any]:
    stream = io.StringIO()
    code = harness.main(["yardstick", str(cohort), str(run), "--groups", str(groups),
                         "--out", str(out), *extra], out=stream)
    assert code == 0, stream.getvalue()
    return json.loads((out / "yardstick.json").read_text(encoding="utf-8"))


def _by_pair(report: dict[str, Any]) -> dict[tuple[int, int], dict[str, Any]]:
    return {(entry["lo"], entry["hi"]): entry for entry in report["pairs"]}


# --- every outcome, produced by the real pipeline -----------------------------------------


def test_each_operator_pair_gets_the_engine_s_own_outcome(
    cohort: Path, run_dir: Path, tmp_path: Path
) -> None:
    report = _yardstick(cohort, run_dir, _groups(tmp_path), tmp_path / "y")
    pairs = _by_pair(report)
    assert pairs[(DUP_A, DUP_B)]["outcome"] == Y.TOGETHER
    assert pairs[(DUP_A, DUP_B)]["zone"] == "merge"
    assert pairs[(DUP_A, DUP_B)]["certificate"] == "K-C"

    sale = pairs[(SALE, RENT)]
    assert sale["outcome"] == Y.BLOCKING_VETO and sale["blocking_veto"] == "category_type"
    assert sale["would"]["zone"] == "veto"

    tie = pairs[tuple(PROJECT_TIE)]
    assert tie["outcome"] == Y.BAND and tie["reason"] == "developer_signature"
    assert tie["stored"] is True

    reject = pairs[FILLER_REJECT]
    assert reject["outcome"] == Y.REJECT and reject["stored"] is True

    never = pairs[NEVER]
    assert never["outcome"] == Y.NEVER_PAIRED
    assert never["never_paired_cause"] in (Y.CAUSE_NO_SHARED_KEY, Y.CAUSE_EXPLODED_KEY,
                                           Y.CAUSE_FANOUT_CAP)
    assert never["would"] is not None and never["stored"] is False


def test_the_headline_counts_pairs_groups_and_coverage(
    cohort: Path, run_dir: Path, tmp_path: Path
) -> None:
    report = _yardstick(cohort, run_dir, _groups(tmp_path), tmp_path / "y")
    assert report["coverage"] == {"pairs_in_cohort": 5, "pairs_one_side_absent": 1,
                                  "pairs_both_absent": 0, "groups_in_cohort": 5}
    head = report["headline"]
    assert head["pairs"] == 5 and head["together"] == 1 and head["missed"] == 4
    assert head["together_share"] == pytest.approx(0.2)
    assert (head["groups_whole"], head["groups_partial"], head["groups_none"]) == (1, 0, 4)
    outcomes = {name: row["n"] for name, row in report["by_outcome"].items()}
    assert outcomes == {Y.TOGETHER: 1, Y.NEVER_PAIRED: 1, Y.BLOCKING_VETO: 1, Y.REJECT: 1,
                        Y.VETO: 0, Y.BAND: 1, Y.SPLIT: 0}
    assert report["blocking_vetoes"] == {"category_type": 1}
    assert sum(report["never_paired_causes"].values()) == 1


def test_the_tables_break_down_by_category_and_source_pair(
    cohort: Path, run_dir: Path, tmp_path: Path
) -> None:
    report = _yardstick(cohort, run_dir, _groups(tmp_path), tmp_path / "y")
    by_category = report["by_category_type"]
    assert sum(row["n"] for row in by_category.values()) == 5
    assert "prodej|pronajem" in by_category  # the sale/rental pair is its own bucket
    for row in by_category.values():
        assert row["n"] == sum(row[outcome] for outcome in Y.OUTCOMES)
    assert sum(row["n"] for row in report["by_source_pair"].values()) == 5


def test_the_miss_list_carries_both_sides_and_the_facts(
    cohort: Path, run_dir: Path, tmp_path: Path
) -> None:
    report = _yardstick(cohort, run_dir, _groups(tmp_path), tmp_path / "y")
    misses = report["misses"]
    assert [entry["outcome"] for entry in misses] == [
        Y.NEVER_PAIRED, Y.BLOCKING_VETO, Y.REJECT, Y.BAND]
    for entry in misses:
        assert len(entry["sides"]) == 2
        for side in entry["sides"]:
            assert {"id", "source", "price", "area_m2", "disposition", "floor"} <= set(side)
        assert isinstance(entry["facts"], list) and isinstance(entry["cluster_facts"], list)
        assert isinstance(entry["cluster_relation_ok"], bool)
    sale = next(entry for entry in misses if entry["outcome"] == Y.BLOCKING_VETO)
    assert "category_type" in sale["facts"]


def test_a_merge_a_cluster_invariant_refuses_is_split_by_invariant(
    cohort: Path, tmp_path: Path
) -> None:
    must_not_link = tmp_path / "mnl.json"
    must_not_link.write_text(json.dumps([[DUP_A, DUP_B]]), encoding="utf-8")
    run = _run(cohort, tmp_path / "r_mnl", "--must-not-link", str(must_not_link))
    report = _yardstick(cohort, run, _groups(tmp_path), tmp_path / "y")
    dup = _by_pair(report)[(DUP_A, DUP_B)]
    assert dup["zone"] == "merge" and dup["outcome"] == Y.SPLIT
    assert dup["refused_by"] == ["must_not_link"]
    assert report["refused_by"] == {"must_not_link": 1}


def test_a_reject_below_the_store_floor_is_re_decided(cohort: Path, tmp_path: Path) -> None:
    settings = tmp_path / "floor.json"
    settings.write_text(json.dumps({"store_floor": 0.3}), encoding="utf-8")
    run = _run(cohort, tmp_path / "r_floor", "--settings", str(settings))
    report = _yardstick(cohort, run, _groups(tmp_path), tmp_path / "y")
    reject = _by_pair(report)[FILLER_REJECT]
    assert reject["outcome"] == Y.REJECT and reject["stored"] is False
    assert reject["zone"] == "reject" and reject["reason"]


# --- the operator's groups, in every shape -------------------------------------------------


def test_the_labels_lane_group_file_is_read_with_each_pair_s_standing(
    cohort: Path, run_dir: Path, tmp_path: Path
) -> None:
    """A pair the operator separated by hand since the merge is not measured — the operator's
    later word is the word — and a group the operator took apart is skipped unless asked."""
    path = tmp_path / "operator_merges.jsonl"
    rows = [
        {"merge_group_id": "g1", "status": "live", "members": [
            {"listing_id": DUP_A, "side": 1, "property_id_at_copy": 7},
            {"listing_id": DUP_B, "side": 2, "property_id_at_copy": 7}],
         "pairs": [{"listing_lo": DUP_A, "listing_hi": DUP_B,
                    "standing": STANDING_BROWSE_MERGE, "verdict": "same"}]},
        {"merge_group_id": "g2", "status": "live", "members": [],
         "pairs": [{"listing_lo": PROJECT_TIE[0], "listing_hi": PROJECT_TIE[1],
                    "standing": STANDING_EXPLICIT, "verdict": "different"}]},
        {"merge_group_id": "g3", "status": "undone", "members": [],
         "pairs": [{"listing_lo": SALE, "listing_hi": RENT, "standing": "unruled"}]},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    report = _yardstick(cohort, run_dir, path, tmp_path / "y")
    assert set(_by_pair(report)) == {(DUP_A, DUP_B)}
    assert report["operator"]["pairs_contradicted_skipped"] == 1
    assert report["operator"]["groups_not_live_skipped"] == 1
    everything = _yardstick(cohort, run_dir, path, tmp_path / "y2", "--include-not-live")
    assert set(_by_pair(everything)) == {(DUP_A, DUP_B), (SALE, RENT)}


def test_browse_merge_label_rows_are_read_and_other_sources_only_on_request(
    cohort: Path, run_dir: Path, tmp_path: Path
) -> None:
    path = tmp_path / "operator_labels.jsonl"
    rows = [
        {"listing_lo": DUP_A, "listing_hi": DUP_B, "verdict": "same", "source": "browse_merge",
         "merge_group_id": "g-1"},
        {"listing_lo": SALE, "listing_hi": RENT, "verdict": "same", "source": "explicit"},
        {"listing_lo": PROJECT_TIE[0], "listing_hi": PROJECT_TIE[1], "verdict": "same",
         "source": "implied", "cluster_key": 900},
        {"listing_lo": NEVER[0], "listing_hi": NEVER[1], "verdict": "different",
         "source": "browse_merge", "merge_group_id": "g-2"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    report = _yardstick(cohort, run_dir, path, tmp_path / "y")
    assert set(_by_pair(report)) == {(DUP_A, DUP_B)}
    assert _by_pair(report)[(DUP_A, DUP_B)]["merge_group_id"] == "g-1"
    wider = _yardstick(cohort, run_dir, path, tmp_path / "y2", "--label-source", "browse_merge",
                       "--label-source", "explicit", "--label-source", "implied")
    assert set(_by_pair(wider)) == {(DUP_A, DUP_B), (SALE, RENT), tuple(PROJECT_TIE)}
    assert _by_pair(wider)[tuple(PROJECT_TIE)]["merge_group_id"] == "cluster:900"


def test_a_pairs_file_of_bare_pairs_is_one_group(
    cohort: Path, run_dir: Path, tmp_path: Path
) -> None:
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps({"pairs": [[DUP_B, DUP_A], [SALE, RENT]]}), encoding="utf-8")
    report = _yardstick(cohort, run_dir, path, tmp_path / "y")
    assert set(_by_pair(report)) == {(DUP_A, DUP_B), (SALE, RENT)}


def test_the_report_files_and_the_printed_table(
    cohort: Path, run_dir: Path, tmp_path: Path
) -> None:
    stream = io.StringIO()
    code = harness.main(["yardstick", str(cohort), str(run_dir / harness.PAIRS_FILE),
                         "--groups", str(_groups(tmp_path)), "--out", str(tmp_path / "y"),
                         "--top", "2"], out=stream)
    assert code == 0
    text = stream.getvalue()
    assert "together 1 of 5 (20.0%)" in text
    assert "MISS LIST (top 2 of 4)" in text
    assert "pair_veto:category_type" in text or "never_paired" in text
    report = json.loads((tmp_path / "y" / "yardstick.json").read_text(encoding="utf-8"))
    assert report["yardstick_version"] == Y.YARDSTICK_VERSION
    assert report["inputs"]["model_version"]
    assert (tmp_path / "y" / "yardstick.md").read_text(encoding="utf-8").startswith("# Yardstick")


def test_missing_inputs_are_refused(cohort: Path, run_dir: Path, tmp_path: Path) -> None:
    assert harness.main(["yardstick", str(cohort), str(run_dir), "--groups",
                         str(tmp_path / "nope.jsonl")], out=io.StringIO()) == 1
    assert harness.main(["yardstick", str(cohort), str(tmp_path / "nowhere"), "--groups",
                         str(_groups(tmp_path))], out=io.StringIO()) == 1


# --- the group model: 560's side rule, in one place ----------------------------------------


def test_merge_pairs_is_560_s_rule() -> None:
    # 1 and 2 came from property 10, 3 from 20, 4 from 30 and was detached since (on 99).
    pairs = merge_pairs([1, 2, 3, 4], [10, 10, 20, 30], [50, 50, 50, 99])
    assert pairs == [(1, 3), (2, 3)]
    assert merge_pairs([1, 2, 3], [10, 10, 20]) == [(1, 3), (2, 3)]
    with pytest.raises(ValueError):
        merge_pairs([1, 2], [10])


def test_a_table_dump_row_and_a_lane_row_parse_to_the_same_pairs(tmp_path: Path) -> None:
    dumped = parse_operator_merge(_table_row("g", [5, 6, 7], [1, 2, 2]))
    assert [pair.key for pair in dumped.ruled_pairs] == [(5, 6), (5, 7)]
    path = tmp_path / "merges.json"
    path.write_text(json.dumps({"groups": [_table_row("g", [5, 6, 7], [1, 2, 2])]}),
                    encoding="utf-8")
    assert [pair.key for pair in load_operator_merges(path)[0].pairs] == [(5, 6), (5, 7)]
