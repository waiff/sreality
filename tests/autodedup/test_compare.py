"""`autodedup.compare` against synthetic judgement files — no network, no database.

The arithmetic is pinned to hand-computed values rather than to whatever the code returns:
an agreement report whose kappa is "whatever came out" is a number the operator cannot act on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autodedup import compare


def _write(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    return path


def _row(lo: int, hi: int, verdict: str, *, tier: str = "oss", model: str = "oss:m",
         stratum: str = "s1", confidence: float | None = 0.8,
         cost: float | None = 0.002, latency: float | None = 1.5,
         **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "lo": lo, "hi": hi, "tier": tier, "model": model, "stratum": stratum,
        "cost_usd": cost, "latency_s": latency,
        "verdict": {"verdict": verdict, "confidence": confidence},
    }
    row.update(extra)
    return row


def _gold(lo: int, hi: int, verdict: str, **kw: Any) -> dict[str, Any]:
    kw.setdefault("tier", "gold")
    kw.setdefault("model", "gpt-5-mini+gpt-5-mini+qwen")
    kw.setdefault("n_votes", 3)
    kw.setdefault("cost", 0.018)
    return _row(lo, hi, verdict, **kw)


SAME = "same_property"
DIFF = "different_property"
BUILDING = "same_building_different_unit"
NONE = "insufficient_evidence"


# --- the arithmetic ---------------------------------------------------------------------


def test_cohen_kappa_matches_a_hand_computed_value() -> None:
    # 10 pairs, two labels, 8 agreements; both raters call A five times and B five times.
    # po = 0.80, pe = 0.5*0.5 + 0.5*0.5 = 0.50, kappa = (0.80-0.50)/0.50 = 0.60.
    pairs = (
        [("A", "A")] * 4 + [("A", "B")] + [("B", "B")] * 4 + [("B", "A")]
    )
    assert compare.agreement(pairs) == pytest.approx(0.8)
    assert compare.cohen_kappa(pairs) == pytest.approx(0.6)


def test_total_agreement_on_one_label_is_kappa_zero_not_one() -> None:
    """Chance agreement is 100% when both sides only ever say one thing — that proves
    nothing about the arm, and 1.0 would claim it proves everything."""
    assert compare.agreement([("A", "A")] * 20) == pytest.approx(1.0)
    assert compare.cohen_kappa([("A", "A")] * 20) == 0.0


def test_perfect_agreement_over_two_labels_is_kappa_one() -> None:
    pairs = [("A", "A")] * 5 + [("B", "B")] * 5
    assert compare.cohen_kappa(pairs) == pytest.approx(1.0)


def test_kappa_and_agreement_are_none_on_an_empty_comparison() -> None:
    assert compare.cohen_kappa([]) is None
    assert compare.agreement([]) is None


# --- one arm ----------------------------------------------------------------------------


def _arm_report(tmp_path: Path, gold_rows: list[dict[str, Any]],
                arm_rows: list[dict[str, Any]]) -> dict[str, Any]:
    gold = compare.load_rows(_write(tmp_path / "gold.jsonl", gold_rows))
    arm = compare.load_rows(_write(tmp_path / "arm.jsonl", arm_rows))
    return compare.compare_arm("arm", tmp_path / "arm.jsonl", arm, gold)


def test_insufficient_is_excluded_from_the_binary_score_and_counted(tmp_path: Path) -> None:
    gold_rows = [_gold(1, 2, SAME), _gold(1, 3, DIFF), _gold(1, 4, SAME), _gold(1, 5, DIFF)]
    arm_rows = [
        _row(1, 2, SAME), _row(1, 3, DIFF),
        _row(1, 4, NONE),      # the arm abstained
        _row(1, 5, DIFF),
    ]
    report = _arm_report(tmp_path, gold_rows, arm_rows)

    assert report["four_way"]["n"] == 4
    assert report["four_way"]["agreement"] == pytest.approx(0.75)
    assert report["binary"]["n"] == 3
    assert report["binary"]["excluded_insufficient"] == 1
    assert report["binary"]["agreement"] == pytest.approx(1.0)
    assert report["insufficient_rate"] == pytest.approx(0.25)
    assert report["gold_insufficient_rate"] == 0.0


def test_same_building_different_unit_is_not_the_same_property(tmp_path: Path) -> None:
    report = _arm_report(
        tmp_path,
        [_gold(1, 2, DIFF), _gold(1, 3, SAME)],
        [_row(1, 2, BUILDING), _row(1, 3, SAME)],
    )
    assert report["four_way"]["agreement"] == pytest.approx(0.5)
    assert report["binary"]["n"] == 2
    assert report["binary"]["agreement"] == pytest.approx(1.0)


def test_per_stratum_agreement_splits_the_headline(tmp_path: Path) -> None:
    gold_rows = [
        _gold(1, 2, SAME, stratum="catalog-only"),
        _gold(1, 3, SAME, stratum="catalog-only"),
        _gold(1, 4, DIFF, stratum="band|model|o1|cross"),
    ]
    arm_rows = [
        _row(1, 2, DIFF, stratum="catalog-only"),
        _row(1, 3, DIFF, stratum="catalog-only"),
        _row(1, 4, DIFF, stratum="band|model|o1|cross"),
    ]
    report = _arm_report(tmp_path, gold_rows, arm_rows)
    assert report["four_way"]["agreement"] == pytest.approx(1 / 3, abs=1e-4)
    assert report["per_stratum"]["catalog-only"] == {"n": 2, "agreement": 0.0}
    assert report["per_stratum"]["band|model|o1|cross"] == {"n": 1, "agreement": 1.0}


def test_confusion_is_gold_by_arm(tmp_path: Path) -> None:
    report = _arm_report(
        tmp_path,
        [_gold(1, 2, SAME), _gold(1, 3, SAME), _gold(1, 4, DIFF)],
        [_row(1, 2, SAME), _row(1, 3, DIFF), _row(1, 4, DIFF)],
    )
    assert report["confusion"] == {SAME: {SAME: 1, DIFF: 1}, DIFF: {DIFF: 1}}


def test_cost_latency_and_confidence_are_per_compared_pair(tmp_path: Path) -> None:
    report = _arm_report(
        tmp_path,
        [_gold(1, 2, SAME), _gold(1, 3, DIFF)],
        [
            _row(1, 2, SAME, cost=0.004, latency=2.0, confidence=0.9),
            _row(1, 3, DIFF, cost=0.002, latency=4.0, confidence=0.7),
        ],
    )
    assert report["cost"]["total_usd"] == pytest.approx(0.006)
    assert report["cost"]["per_pair_usd"] == pytest.approx(0.003)
    assert report["latency"]["mean_s"] == pytest.approx(3.0)
    assert report["latency"]["p95_s"] == pytest.approx(4.0)
    assert report["mean_confidence"] == pytest.approx(0.8)


def test_a_missing_cost_is_not_a_free_pair(tmp_path: Path) -> None:
    report = _arm_report(
        tmp_path,
        [_gold(1, 2, SAME), _gold(1, 3, DIFF)],
        [_row(1, 2, SAME, cost=0.004), _row(1, 3, DIFF, cost=None)],
    )
    assert report["cost"]["n_priced"] == 1
    assert report["cost"]["per_pair_usd"] == pytest.approx(0.004)


def test_pairs_gold_never_saw_are_left_out_and_counted(tmp_path: Path) -> None:
    report = _arm_report(
        tmp_path,
        [_gold(1, 2, SAME), _gold(1, 3, DIFF), _gold(1, 9, DIFF)],
        [_row(1, 2, SAME), _row(1, 3, DIFF), _row(1, 7, SAME)],
    )
    assert report["n_compared"] == 2
    assert report["n_missing"] == 1
    assert report["n_judged"] == 3


# --- reading the files -------------------------------------------------------------------


def test_the_gold_aggregate_beats_the_votes_it_was_built_from(tmp_path: Path) -> None:
    """A gold run writes both: three per-vote rows and the aggregate, all under tier gold."""
    rows = [
        _row(1, 2, DIFF, tier="gold", model="gpt-5-mini", strategy="matched_first"),
        _row(1, 2, SAME, tier="gold", model="gpt-5-mini", strategy="sequence_first"),
        _row(1, 2, SAME, tier="gold", model="qwen3-vl-30b-a3b-instruct"),
        _gold(1, 2, SAME),
    ]
    loaded = compare.load_rows(_write(tmp_path / "gold.jsonl", rows))
    assert loaded[(1, 2)].verdict == SAME
    assert loaded[(1, 2)].aggregate is True


def test_an_incomplete_gold_pair_carries_no_verdict_and_is_skipped(tmp_path: Path) -> None:
    rows = [
        _gold(1, 2, SAME),
        {"lo": 1, "hi": 3, "tier": "gold", "n_votes": 1, "incomplete": True,
         "cost_usd": 0.007},
    ]
    loaded = compare.load_rows(_write(tmp_path / "gold.jsonl", rows))
    assert list(loaded) == [(1, 2)]


def test_a_file_without_a_single_verdict_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path / "empty.jsonl", [{"lo": 1, "hi": 2, "tier": "gold",
                                              "incomplete": True}])
    with pytest.raises(SystemExit):
        compare.load_rows(path)


# --- the cli -----------------------------------------------------------------------------


def _cli(tmp_path: Path, gold_rows: list[dict[str, Any]],
         arms: dict[str, list[dict[str, Any]]]) -> tuple[int, dict[str, Any], str]:
    gold_path = _write(tmp_path / "gold.jsonl", gold_rows)
    argv = ["--gold", str(gold_path), "--out", str(tmp_path / "report")]
    for name, rows in arms.items():
        path = _write(tmp_path / f"{name}.jsonl", rows)
        argv += ["--arm", f"{name}={path}"]
    code = compare.run(argv)
    report = json.loads(
        (tmp_path / "report" / compare.REPORT_JSON).read_text(encoding="utf-8")
    )
    markdown = (tmp_path / "report" / compare.REPORT_MD).read_text(encoding="utf-8")
    return code, report, markdown


def test_the_cli_reports_every_arm_and_the_agreement_between_them(tmp_path: Path) -> None:
    gold_rows = [_gold(1, 2, SAME), _gold(1, 3, DIFF), _gold(1, 4, DIFF), _gold(1, 5, SAME)]
    code, report, markdown = _cli(tmp_path, gold_rows, {
        "paid": [
            _row(1, 2, SAME, tier="vision", model="gpt-5-mini", cost=0.006, latency=6.0),
            _row(1, 3, DIFF, tier="vision", model="gpt-5-mini", cost=0.006, latency=6.0),
            _row(1, 4, DIFF, tier="vision", model="gpt-5-mini", cost=0.006, latency=6.0),
            _row(1, 5, SAME, tier="vision", model="gpt-5-mini", cost=0.006, latency=6.0),
        ],
        "oss": [
            _row(1, 2, SAME), _row(1, 3, DIFF), _row(1, 4, SAME), _row(1, 5, SAME),
        ],
    })
    assert code == 0
    assert report["gold"]["n_pairs"] == 4
    by_name = {arm["name"]: arm for arm in report["arms"]}
    assert by_name["paid"]["four_way"]["agreement"] == pytest.approx(1.0)
    assert by_name["paid"]["four_way"]["kappa"] == pytest.approx(1.0)
    assert by_name["oss"]["four_way"]["agreement"] == pytest.approx(0.75)
    assert by_name["oss"]["cost"]["per_pair_usd"] == pytest.approx(0.002)
    assert by_name["paid"]["cost"]["per_pair_usd"] == pytest.approx(0.006)

    between = report["between_arms"]
    assert len(between) == 1
    assert between[0]["n"] == 4
    assert between[0]["agreement"] == pytest.approx(0.75)

    assert "# Judge arms vs gold" in markdown
    assert "## Between arms" in markdown
    assert "| oss |" in markdown


def test_an_arm_sharing_no_pair_with_gold_exits_non_zero(tmp_path: Path) -> None:
    code, report, _ = _cli(
        tmp_path,
        [_gold(1, 2, SAME)],
        {"stray": [_row(9, 9, SAME)]},
    )
    assert code == 1
    assert report["arms"][0]["n_compared"] == 0
    assert report["arms"][0]["four_way"]["agreement"] is None


def test_an_arm_spec_must_be_name_equals_path() -> None:
    with pytest.raises(SystemExit):
        compare.parse_arm("just-a-path.jsonl")


def test_the_cost_column_survives_the_rounding(tmp_path: Path) -> None:
    """A pod's share is fractions of a cent a pair and the paid vision arm about $0.002 — the
    generic three-decimal float renders both as `0.000`, erasing the cost ratio this whole
    comparison exists to expose from the one table the operator reads."""
    _, report, markdown = _cli(
        tmp_path,
        [_gold(1, 2, SAME), _gold(3, 4, DIFF)],
        {
            "pod": [_row(1, 2, SAME, cost=0.00042), _row(3, 4, DIFF, cost=0.00042)],
            "paid": [_row(1, 2, SAME, tier="vision", cost=0.00568),
                     _row(3, 4, DIFF, tier="vision", cost=0.00568)],
        },
    )
    assert report["arms"][0]["cost"]["per_pair_usd"] == pytest.approx(0.00042)
    assert "0.00042" in markdown and "0.00568" in markdown
    # The two arms are 13x apart in price; at three decimals both cells read `0.000`.
    assert "| 0.000 | 0.000 |" not in markdown


def test_a_priceless_arm_still_renders_a_cell() -> None:
    assert compare._usd(None) == "–"
