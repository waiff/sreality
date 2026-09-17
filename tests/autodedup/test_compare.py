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


# --- gold votes -------------------------------------------------------------------------
#
# One synthetic gold file, five pairs, every number below computed by hand from it.
#
#   pair   primary v1  primary v2  third vote                 aggregate
#   (1,2)  same        same        qwen same                  same
#   (3,4)  diff        diff        qwen insufficient (raw diff) diff
#   (5,6)  same        diff        qwen same                  same        <- primary split
#   (7,8)  same        same        weaker gpt-5-mini same     same        <- fallback, no qwen
#   (9,10) diff        diff        qwen same                  diff
#
# Covered (two primary votes AND a secondary vote): (1,2) (3,4) (5,6) (9,10).
# Unanimous within that: (1,2) (3,4) (9,10).

QWEN = "qwen-vl"
MINI = "gpt-5-mini"
MISSING = compare.MISSING_DISCRIMINATOR


def _vote(lo: int, hi: int, model: str, strategy: str, verdict: str, *,
          weaker: bool = False, downgraded_from: str | None = None,
          discriminator: str | None = "floor 2 vs floor 8",
          cost: float = 0.004, confidence: float = 0.9) -> dict[str, Any]:
    return {
        "lo": lo, "hi": hi, "tier": "gold", "model": model, "strategy": strategy,
        "stratum": "s1", "weaker": weaker, "cost_usd": cost, "llm_call_id": lo * 100 + hi,
        "verdict": {
            "verdict": verdict, "confidence": confidence,
            "downgraded_from": downgraded_from,
            "unit_discriminator": discriminator,
        },
    }


def _consensus(lo: int, hi: int, verdict: str, models: str, *,
               cost: float = 0.010, weaker: bool = False) -> dict[str, Any]:
    return {
        "lo": lo, "hi": hi, "tier": "gold", "model": models, "stratum": "s1",
        "n_votes": 3, "cost_usd": cost, "weaker": weaker,
        "verdict": {"verdict": verdict, "confidence": 1.0},
    }


THREE = f"{MINI}+{MINI}+{QWEN}"
WEAKER_MODELS = f"{MINI}+{MINI}+{MINI} (weaker)"


def _gold_vote_rows() -> list[dict[str, Any]]:
    return [
        _vote(1, 2, MINI, "matched_first", SAME),
        _vote(1, 2, MINI, "sequence_first", SAME),
        _vote(1, 2, QWEN, "matched_first", SAME, cost=0.002),
        _consensus(1, 2, SAME, THREE),

        _vote(3, 4, MINI, "matched_first", DIFF),
        _vote(3, 4, MINI, "sequence_first", DIFF),
        # The arm judged, then E27 rewrote it: a non-same verdict that named no discriminator.
        _vote(3, 4, QWEN, "matched_first", NONE, downgraded_from=DIFF,
              discriminator=MISSING, cost=0.002),
        _consensus(3, 4, DIFF, THREE),

        _vote(5, 6, MINI, "matched_first", SAME),
        _vote(5, 6, MINI, "sequence_first", DIFF),
        _vote(5, 6, QWEN, "matched_first", SAME, cost=0.002),
        _consensus(5, 6, SAME, THREE),

        _vote(7, 8, MINI, "matched_first", SAME),
        _vote(7, 8, MINI, "sequence_first", SAME),
        _vote(7, 8, MINI, "sequence_first", SAME, weaker=True),
        _consensus(7, 8, SAME, WEAKER_MODELS, weaker=True),

        _vote(9, 10, MINI, "matched_first", DIFF),
        _vote(9, 10, MINI, "sequence_first", DIFF),
        _vote(9, 10, QWEN, "matched_first", SAME, cost=0.002),
        _consensus(9, 10, DIFF, THREE),
    ]


def _gold_votes_report(tmp_path: Path, rows: list[dict[str, Any]] | None = None,
                       summary: dict[str, Any] | None = None) -> dict[str, Any]:
    path = _write(tmp_path / "judgements.jsonl", rows if rows is not None
                  else _gold_vote_rows())
    if summary is not None:
        (tmp_path / compare.SUMMARY_NAME).write_text(
            json.dumps({"result": summary}), encoding="utf-8"
        )
    pairs = compare.load_gold_pairs(path)
    return compare.build_gold_votes_report(
        [(path, pairs, compare.load_run_summary(path))]
    )


def test_gold_votes_load_splits_the_aggregate_from_the_votes(tmp_path: Path) -> None:
    pairs = compare.load_gold_pairs(_write(tmp_path / "g.jsonl", _gold_vote_rows()))
    by_key = {(pair.lo, pair.hi): pair for pair in pairs}
    assert len(pairs) == 5
    assert by_key[(1, 2)].aggregate == SAME
    assert [vote.model for vote in by_key[(1, 2)].votes] == [MINI, MINI, QWEN]
    # Plan order is kept, so vote 1 is the matched-first vote.
    assert by_key[(1, 2)].votes[0].strategy == "matched_first"


def test_the_raw_verdict_is_what_the_model_said_before_the_downgrade(tmp_path: Path) -> None:
    pairs = compare.load_gold_pairs(_write(tmp_path / "g.jsonl", _gold_vote_rows()))
    arm = [v for p in pairs if (p.lo, p.hi) == (3, 4) for v in p.votes if v.model == QWEN][0]
    assert arm.verdict == NONE
    assert arm.raw_verdict == DIFF
    assert arm.downgraded_from == DIFF
    assert arm.no_discriminator is True


def test_the_primary_arm_is_the_one_that_votes_twice(tmp_path: Path) -> None:
    pairs = compare.load_gold_pairs(_write(tmp_path / "g.jsonl", _gold_vote_rows()))
    assert compare.resolve_arms(pairs) == (MINI, [QWEN])


def test_a_pair_without_a_secondary_vote_is_not_compared(tmp_path: Path) -> None:
    report = _gold_votes_report(tmp_path)
    assert report["totals"]["n_pairs"] == 5
    # (7,8) took the weaker fallback: there is no independent arm on it to score.
    assert report["totals"]["n_with_secondary"] == 4
    assert report["totals"]["n_fallback_pairs"] == 1
    assert report["f_fallback"]["fallback"]["rate"] == pytest.approx(0.2)


def test_stored_and_raw_agreement_are_both_reported(tmp_path: Path) -> None:
    report = _gold_votes_report(tmp_path)
    stored = (report["views"]["stored"]["a_secondary_vs_primary_majority"]
              ["primary_unanimous"])
    raw = report["views"]["raw"]["a_secondary_vs_primary_majority"]["primary_unanimous"]
    # Three unanimous pairs; stored the arm matches only (1,2), raw it also matches (3,4).
    assert stored["four_way"]["n"] == raw["four_way"]["n"] == 3
    assert stored["four_way"]["agreement"] == pytest.approx(1 / 3, abs=5e-5)
    assert raw["four_way"]["agreement"] == pytest.approx(2 / 3, abs=5e-5)
    # po = 1/3; pe = (1/3)(2/3) = 2/9; kappa = (1/3 - 2/9) / (1 - 2/9) = 1/7.
    assert stored["four_way"]["kappa"] == pytest.approx(1 / 7, abs=5e-5)


def test_the_binary_score_drops_the_downgraded_abstention_and_counts_it(
    tmp_path: Path,
) -> None:
    stored = (_gold_votes_report(tmp_path)["views"]["stored"]
              ["a_secondary_vs_primary_majority"]["primary_unanimous"]["binary"])
    # (3,4) is excluded as an abstention; (1,2) agrees and (9,10) does not.
    assert stored["n"] == 2
    assert stored["excluded_insufficient"] == 1
    assert stored["agreement"] == pytest.approx(0.5)


def test_a_wilson_interval_is_attached_to_every_rate(tmp_path: Path) -> None:
    entry = (_gold_votes_report(tmp_path)["views"]["stored"]
             ["a_secondary_vs_primary_majority"]["primary_unanimous"]["four_way"])
    low, high = entry["ci95"]
    assert 0.0 < low < entry["agreement"] < high < 1.0


def test_the_secondary_arms_same_property_calls_are_scored_both_ways(
    tmp_path: Path,
) -> None:
    """Precision is the merge-safety number and recall the duplicates left behind; an arm that
    abstains its way to a clean precision has to show the recall beside it."""
    block = (_gold_votes_report(tmp_path)["views"]["stored"]
             ["a_secondary_vs_primary_majority"]["secondary_same_property"])
    # The arm called same_property on (1,2) — right — and on (9,10) — wrong.
    assert block["precision"]["k"] == 1 and block["precision"]["n"] == 2
    # The paid pair agreed on same_property once, on (1,2), and the arm found it.
    assert block["recall"]["k"] == 1 and block["recall"]["n"] == 1


def test_a_split_primary_pair_is_reported_apart_from_the_agreement(tmp_path: Path) -> None:
    split = (_gold_votes_report(tmp_path)["views"]["stored"]
             ["a_secondary_vs_primary_majority"]["primary_split"])
    assert split["n"] == 1
    assert split["secondary_matched_one_vote"]["k"] == 1


def test_the_inter_rater_baseline_is_the_primary_against_itself(tmp_path: Path) -> None:
    block = _gold_votes_report(tmp_path)["views"]["stored"]["c_primary_vs_primary"]
    assert block["all_pairs"]["four_way"]["n"] == 5
    assert block["all_pairs"]["four_way"]["agreement"] == pytest.approx(0.8)
    # The shared subset drops the fallback pair, which the secondary arm never saw.
    assert block["shared_subset"]["four_way"]["n"] == 4
    assert block["shared_subset"]["four_way"]["agreement"] == pytest.approx(0.75)


def test_agreement_with_the_aggregate_is_flagged_as_self_inclusive(tmp_path: Path) -> None:
    block = _gold_votes_report(tmp_path)["views"]["stored"]["b_secondary_vs_aggregate"]
    assert block["four_way"]["n"] == 4
    assert block["four_way"]["agreement"] == pytest.approx(0.5)
    assert "contains this vote" in block["note"]


def test_what_the_third_vote_actually_changed_is_counted(tmp_path: Path) -> None:
    influence = _gold_votes_report(tmp_path)["b2_aggregate_influence"]
    # Only (5,6): the paid votes split, so on their own they would have called it no-majority.
    assert influence["changed"]["k"] == 1 and influence["changed"]["n"] == 4
    assert influence["transitions"] == {f"{NONE} -> {SAME}": 1}


def test_the_downgrade_rate_is_reported_per_model(tmp_path: Path) -> None:
    downgrades = _gold_votes_report(tmp_path)["downgrades"]
    assert downgrades[MINI]["downgraded"]["k"] == 0
    assert downgrades[QWEN]["votes"] == 4
    assert downgrades[QWEN]["downgraded"]["k"] == 1
    assert downgrades[QWEN]["no_unit_discriminator"]["k"] == 1
    assert downgrades[QWEN]["transitions"] == {f"{DIFF} -> {NONE}": 1}


def test_the_paired_lean_counts_only_the_discordant_pairs(tmp_path: Path) -> None:
    mix = _gold_votes_report(tmp_path)["views"]["stored"]["d_verdict_mix"]
    same_bias = mix["paired_same_bias"]
    # (9,10): the arm says same where the unanimous paid pair says different; never the reverse.
    assert same_bias["arm_only"] == 1 and same_bias["reference_only"] == 0
    assert same_bias["share_arm"] == pytest.approx(1.0)


def test_cost_is_per_vote_and_the_ratio_is_arm_over_paid(tmp_path: Path) -> None:
    cost = _gold_votes_report(tmp_path)["e_cost"]
    assert cost["per_model"][MINI]["votes"] == 11
    assert cost["per_model"][MINI]["weaker_votes"] == 1
    assert cost["per_model"][QWEN]["votes"] == 4
    assert cost["per_model"][MINI]["cost_usd"]["mean"] == pytest.approx(0.004)
    assert cost["per_model"][QWEN]["cost_usd"]["mean"] == pytest.approx(0.002)
    assert cost["ratio_secondary_over_primary"] == pytest.approx(0.5)


def test_a_call_that_was_billed_and_never_parsed_is_charged_to_the_arm(
    tmp_path: Path,
) -> None:
    """Those calls leave NO row in the jsonl, so the per-vote mean flatters the arm. The
    effective price divides what was spent by the votes that came back usable."""
    cost = _gold_votes_report(
        tmp_path, summary={"billed_unparsed": 4, "errors_total": 4}
    )["e_cost"]
    assert cost["billed_unparsed_calls"] == 4
    # 4 usable votes at $0.002, 4 more paid for and lost: $0.016 / 4.
    assert cost["effective_per_usable_secondary_vote_usd"] == pytest.approx(0.004)


def test_the_fallback_reasons_come_from_the_run_summary(tmp_path: Path) -> None:
    report = _gold_votes_report(tmp_path, summary={
        "errors_total": 352,
        "billed_unparsed": 351,
        "errors": [
            f"1060x18754086 gold/{QWEN}: JudgeParseError: key_evidence must be a list",
            f"318x140923 gold/{QWEN}: JudgeParseError: key_evidence must be a list",
        ],
    })
    reasons = report["f_fallback"]["reasons_listed"]
    assert reasons[0]["model"] == QWEN
    assert reasons[0]["kind"] == "JudgeParseError"
    assert reasons[0]["n_listed"] == 2
    # The lane caps the stored list: the run's own total is the number that counts.
    assert report["files"][0]["errors_total"] == 352


def test_the_gold_votes_cli_writes_both_reports(tmp_path: Path) -> None:
    path = _write(tmp_path / "judgements.jsonl", _gold_vote_rows())
    out = tmp_path / "out"
    code = compare.run(["--gold-votes", "--gold", str(path), "--out", str(out)])
    assert code == 0
    report = json.loads((out / compare.GOLD_VOTES_JSON).read_text())
    markdown = (out / compare.GOLD_VOTES_MD).read_text()
    assert report["arms"] == {"primary": MINI, "secondary": [QWEN]}
    assert "Gold votes" in markdown and "stored" in markdown and "raw" in markdown


def test_gold_votes_pools_several_runs(tmp_path: Path) -> None:
    first = _write(tmp_path / "a.jsonl", _gold_vote_rows())
    second = _write(tmp_path / "b.jsonl", _gold_vote_rows())
    out = tmp_path / "out"
    assert compare.run([
        "--gold-votes", "--gold", str(first), "--gold", str(second), "--out", str(out)
    ]) == 0
    report = json.loads((out / compare.GOLD_VOTES_JSON).read_text())
    assert report["totals"]["n_pairs"] == 10
    assert [row["n_pairs"] for row in report["files"]] == [5, 5]


def test_a_gold_file_with_one_arm_only_exits_non_zero(tmp_path: Path) -> None:
    rows = [row for row in _gold_vote_rows() if row.get("model") != QWEN]
    path = _write(tmp_path / "judgements.jsonl", rows)
    assert compare.run([
        "--gold-votes", "--gold", str(path), "--out", str(tmp_path / "out")
    ]) == 1


def test_arm_comparison_still_takes_exactly_one_gold(tmp_path: Path) -> None:
    gold = _write(tmp_path / "gold.jsonl", [_gold(1, 2, SAME)])
    arm = _write(tmp_path / "arm.jsonl", [_row(1, 2, SAME)])
    with pytest.raises(SystemExit):
        compare.run(["--gold", str(gold), "--gold", str(gold), "--arm", f"a={arm}",
                     "--out", str(tmp_path / "out")])
    with pytest.raises(SystemExit):
        compare.run(["--gold", str(gold), "--out", str(tmp_path / "out")])
