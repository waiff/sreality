"""`--operator-labels`: the operator as the reference an arm is scored against.

The arithmetic here is hand-computed, because the two numbers this mode exists to publish —
the false-merge rate and the missed-duplicate rate — are the ones a rollout decision hangs on,
and a rate that is "whatever came out" cannot carry that.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autodedup import compare

SAME = "same_property"
DIFF = "different_property"
BUILDING = "same_building_different_unit"
NONE = "insufficient_evidence"


def _label(lo: int, hi: int, verdict: str, *, source: str = "explicit",
           zone: str | None = "band") -> dict[str, Any]:
    row: dict[str, Any] = {
        "listing_lo": lo, "listing_hi": hi, "verdict": verdict,
        "relation": verdict, "source": source, "reasons": [], "note": None,
        "must_not_link": False,
    }
    row["engine"] = None if zone is None else {"zone": zone, "score": 0.5}
    return row


def _arm(lo: int, hi: int, verdict: str, *, cost: float = 0.002,
         latency: float = 1.5) -> dict[str, Any]:
    return {
        "lo": lo, "hi": hi, "tier": "vision", "model": "gpt-5-mini", "stratum": "s1",
        "cost_usd": cost, "latency_s": latency,
        "verdict": {"verdict": verdict, "confidence": 0.8},
    }


def _write(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


# --- the relation map -------------------------------------------------------------------


def test_the_four_operator_relations_map_onto_the_judge_vocabulary(tmp_path: Path) -> None:
    path = _write(tmp_path / "ops.jsonl", [
        _label(1, 2, "same"),
        _label(1, 3, "different"),
        _label(1, 4, "same_building_different_unit"),
        _label(1, 5, "same_project_different_unit"),
    ])
    rows = compare.load_operator_labels(path)
    assert rows[(1, 2)].verdict == SAME
    assert rows[(1, 3)].verdict == DIFF
    assert rows[(1, 4)].verdict == BUILDING
    # The judge has no project verdict: for a merge decision it means the same as `different`.
    assert rows[(1, 5)].verdict == DIFF


def test_implied_labels_are_off_unless_asked_for(tmp_path: Path) -> None:
    path = _write(tmp_path / "ops.jsonl", [
        _label(1, 2, "same", source="explicit"),
        _label(1, 3, "same", source="implied"),
    ])
    assert set(compare.load_operator_labels(path)) == {(1, 2)}
    both = compare.load_operator_labels(path, ("explicit", "implied"))
    assert set(both) == {(1, 2), (1, 3)}


def test_the_engine_zone_becomes_the_stratum(tmp_path: Path) -> None:
    path = _write(tmp_path / "ops.jsonl", [
        _label(1, 2, "same", zone="merge"),
        _label(1, 3, "different", zone=None),
    ])
    rows = compare.load_operator_labels(path)
    assert rows[(1, 2)].stratum == "merge"
    # A pair the engine never stored is still a ruling; it is named, not dropped.
    assert rows[(1, 3)].stratum == "unstored"


def test_an_unknown_source_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path / "ops.jsonl", [_label(1, 2, "same")])
    with pytest.raises(SystemExit):
        compare.load_operator_labels(path, ("guessed",))


def test_a_file_with_no_label_of_the_wanted_source_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path / "ops.jsonl", [_label(1, 2, "same", source="implied")])
    with pytest.raises(SystemExit):
        compare.load_operator_labels(path)


# --- the two error rates ----------------------------------------------------------------


def test_false_merges_and_missed_duplicates_have_different_denominators() -> None:
    # 4 reference not-same pairs, the arm merges 1 of them → false merge 1/4.
    # 3 reference same pairs, the arm misses 2 (one as not-same, one as an abstention)
    # → missed duplicate 2/3.
    pairs = [
        (DIFF, SAME), (DIFF, DIFF), (BUILDING, DIFF), (DIFF, NONE),
        (SAME, SAME), (SAME, DIFF), (SAME, NONE),
    ]
    errors = compare.error_rates(pairs)
    assert (errors["false_merge"]["k"], errors["false_merge"]["n"]) == (1, 4)
    assert errors["false_merge"]["rate"] == pytest.approx(0.25)
    assert (errors["missed_duplicate"]["k"], errors["missed_duplicate"]["n"]) == (2, 3)
    assert errors["missed_as_not_same"] == 1
    assert errors["missed_as_abstention"] == 1
    # An abstention links nothing, so it never counts as a false merge — but it is counted.
    assert errors["false_merge_as_abstention"] == 1


def test_a_perfect_arm_still_carries_a_wilson_interval() -> None:
    errors = compare.error_rates([(DIFF, DIFF)] * 10 + [(SAME, SAME)] * 10)
    assert errors["false_merge"]["rate"] == 0.0
    # 0/10 is not proof of zero: the interval says how little 10 pairs settle.
    assert errors["false_merge"]["wilson_high"] > 0.25
    assert errors["missed_duplicate"]["rate"] == 0.0


def test_an_empty_denominator_reports_no_rate() -> None:
    errors = compare.error_rates([(SAME, SAME)])
    assert errors["false_merge"] == {
        "k": 0, "n": 0, "rate": None, "wilson_low": None, "wilson_high": None
    }


# --- end to end through the CLI -----------------------------------------------------------


def test_the_cli_scores_arms_against_the_operator(tmp_path: Path) -> None:
    labels = _write(tmp_path / "ops.jsonl", [
        _label(1, 2, "same", zone="merge"),
        _label(1, 3, "different", zone="band"),
        _label(1, 4, "same", zone="band"),
        _label(1, 5, "same_project_different_unit", zone="reject"),
    ])
    arm = _write(tmp_path / "arm.jsonl", [
        _arm(1, 2, SAME), _arm(1, 3, SAME), _arm(1, 4, NONE), _arm(1, 5, DIFF),
    ])
    out = tmp_path / "out"
    assert compare.run([
        "--operator-labels", str(labels), "--arm", f"vision={arm}", "--out", str(out)
    ]) == 0
    report = json.loads((out / compare.REPORT_JSON).read_text(encoding="utf-8"))
    assert report["reference"] == "operator"
    errors = report["arms"][0]["errors"]
    # Not-same reference pairs: (1,3) and (1,5). The arm merged (1,3).
    assert (errors["false_merge"]["k"], errors["false_merge"]["n"]) == (1, 2)
    # Same reference pairs: (1,2) and (1,4). The arm abstained on (1,4).
    assert (errors["missed_duplicate"]["k"], errors["missed_duplicate"]["n"]) == (1, 2)
    assert errors["missed_as_abstention"] == 1
    zones = report["arms"][0]["per_stratum"]
    assert set(zones) == {"merge", "band", "reject"}
    assert zones["band"]["errors"]["false_merge"]["k"] == 1
    text = (out / compare.REPORT_MD).read_text(encoding="utf-8")
    assert "false merge" in text and "1/2 = 50.00%" in text


def test_giving_both_references_or_neither_is_refused(tmp_path: Path) -> None:
    labels = _write(tmp_path / "ops.jsonl", [_label(1, 2, "same")])
    arm = _write(tmp_path / "arm.jsonl", [_arm(1, 2, SAME)])
    for argv in (
        ["--arm", f"a={arm}", "--out", str(tmp_path / "o")],
        ["--gold", str(arm), "--operator-labels", str(labels),
         "--arm", f"a={arm}", "--out", str(tmp_path / "o")],
    ):
        with pytest.raises(SystemExit) as caught:
            compare.run(argv)
        assert caught.value.code != 0
