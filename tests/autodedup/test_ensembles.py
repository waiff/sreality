"""`autodedup.ensembles` — rules over verdicts that are already stored.

Every number is hand-computed. An ensemble report whose cost is "whatever the code summed" is
exactly the artefact that let W6's gold pass double-book $2.32.
"""

from __future__ import annotations

import pytest

from autodedup import ensembles
from autodedup.ensembles import ArmRow, Rule, catalogue, decide, named, score, score_all

SAME = ensembles.MERGE
DIFF = "different_property"
BUILDING = ensembles.SAME_BUILDING
ABSTAIN = ensembles.ABSTAIN


def _rows(**kw: str) -> dict[str, ArmRow]:
    return {name: ArmRow(verdict, 0.001, 2.0) for name, verdict in kw.items()}


def test_catalogue_covers_singles_unanimity_both_cascade_orders_and_vetoes() -> None:
    rules = catalogue(["a", "b", "c"])
    names = [rule.name for rule in rules]
    assert names.count("a") == 1
    assert "unan(a+b)" in names and "unan(a+b+c)" in names
    # Both directions, because a proposing and b confirming is not the same rule as the reverse.
    assert "casc(a>b)" in names and "casc(b>a)" in names
    assert "unan(a+b)|veto" in names
    # 3 singles + 3 pairs + 1 triple + 6 ordered cascades = 13, doubled by the veto.
    assert len(rules) == 26
    assert len(catalogue(["a", "b", "c"], veto=False)) == 13


def test_unanimity_merges_only_when_every_arm_says_same() -> None:
    rule = named(catalogue(["a", "b"]), "unan(a+b)")
    assert decide(rule, _rows(a=SAME, b=SAME)).merge is True
    assert decide(rule, _rows(a=SAME, b=DIFF)).merge is False
    assert decide(rule, _rows(a=SAME, b=ABSTAIN)).merge is False


def test_a_cascade_pays_its_second_stage_only_on_survivors() -> None:
    rule = named(catalogue(["a", "b"]), "casc(a>b)")
    rows = {"a": ArmRow(SAME, 0.001, 2.0), "b": ArmRow(SAME, 0.005, 20.0)}
    passed = decide(rule, rows)
    assert passed.merge is True
    assert passed.consulted == ("a", "b")
    assert passed.cost_usd == pytest.approx(0.006)
    # Stages are serial, so the latencies add.
    assert passed.latency_s == pytest.approx(22.0)

    stopped = decide(rule, {"a": ArmRow(DIFF, 0.001, 2.0), "b": ArmRow(SAME, 0.005, 20.0)})
    assert stopped.merge is False
    assert stopped.consulted == ("a",)
    assert stopped.cost_usd == pytest.approx(0.001)
    assert stopped.latency_s == pytest.approx(2.0)


def test_the_veto_is_paid_for_up_front_and_fires_on_abstain_or_same_building() -> None:
    rule = named(catalogue(["a", "b"]), "a|veto")
    merged = decide(rule, _rows(a=SAME, b=DIFF))
    assert merged.merge is True
    # The veto arm is consulted even when it does not fire, so its call is charged.
    assert merged.consulted == ("a", "b")
    assert merged.cost_usd == pytest.approx(0.002)
    assert decide(rule, _rows(a=SAME, b=ABSTAIN)).merge is False
    assert decide(rule, _rows(a=SAME, b=BUILDING)).merge is False


def test_a_missing_verdict_leaves_the_pair_uncovered_not_a_no_merge() -> None:
    rule = named(catalogue(["a", "b"]), "unan(a+b)")
    out = decide(rule, {"a": ArmRow(SAME)})
    assert out.covered is False and out.merge is False
    # A cascade that never reaches its second stage does not need it.
    stopped = decide(named(catalogue(["a", "b"]), "casc(a>b)"), {"a": ArmRow(DIFF)})
    assert stopped.covered is True and stopped.merge is False


def test_score_keeps_the_two_errors_apart_and_attributes_them_to_blocks() -> None:
    arms = {
        "a": {
            (1, 2): ArmRow(SAME, 0.001, 2.0),
            (3, 4): ArmRow(SAME, 0.001, 4.0),
            (5, 6): ArmRow(DIFF, 0.001, 6.0),
            (7, 8): ArmRow(SAME, 0.001, 8.0),
        }
    }
    reference = {(1, 2): "not_same", (3, 4): "not_same", (5, 6): "not_same", (7, 8): "same"}
    blocks = {(1, 2): "A", (3, 4): "A", (5, 6): "B", (7, 8): "C"}
    out = score(Rule("a", "single", (("a",),)), arms, reference, blocks)
    assert out["false_merge"]["k"] == 2 and out["false_merge"]["n"] == 3
    assert out["recall"] == {"k": 1, "n": 1, "rate": 1.0,
                             "wilson_low": 0.2065, "wilson_high": 1.0}
    assert out["blocks"]["negative_blocks"] == 2
    assert out["blocks"]["false_merge_blocks"] == 1
    assert out["blocks"]["false_merge_block_names"] == ["A"]
    assert out["latency"]["p50_s"] == 6.0
    assert out["n_covered"] == 4


def test_score_all_covers_the_whole_catalogue_and_an_uncovered_pair_is_dropped() -> None:
    arms = {
        "a": {(1, 2): ArmRow(SAME, 0.001, 1.0), (3, 4): ArmRow(SAME, 0.001, 1.0)},
        "b": {(1, 2): ArmRow(DIFF, 0.004, 9.0)},
    }
    reference = {(1, 2): "not_same", (3, 4): "not_same"}
    rules = {entry["rule"]: entry for entry in score_all(arms, reference)}
    assert rules["a"]["n_covered"] == 2 and rules["a"]["false_merge"]["k"] == 2
    # b never answered (3, 4), so every rule that consults b scores one pair, not two.
    assert rules["unan(a+b)"]["n_covered"] == 1
    assert rules["unan(a+b)"]["false_merge"] == {
        "k": 0, "n": 1, "rate": 0.0, "wilson_low": 0.0, "wilson_high": 0.7935
    }
    assert rules["casc(a>b)"]["n_covered"] == 1


def test_project_monthly_usd_scales_the_population_not_the_price() -> None:
    assert ensembles.project_monthly_usd(0.001, 296_550) == 296.55
    assert ensembles.project_monthly_usd(0.001, 296_550, 0.386) == 114.47
    assert ensembles.project_monthly_usd(None, 296_550) is None


def test_escalation_is_counted_by_stages_run_not_by_arms_paid() -> None:
    """An all-arm veto pays for every arm in stage 0, so `consulted` cannot tell a cascade
    that escalated from one that stopped. Only the stage count can."""
    rule = named(catalogue(["a", "b"]), "casc(a>b)|veto")
    rows = {"a": ArmRow(SAME, 0.001, 1.0), "b": ArmRow(SAME, 0.005, 9.0)}
    escalated = decide(rule, rows)
    assert escalated.stages_run == 2 and escalated.consulted == ("a", "b")
    stopped = decide(rule, {"a": ArmRow(DIFF, 0.001, 1.0), "b": ArmRow(SAME, 0.005, 9.0)})
    assert stopped.stages_run == 1 and stopped.consulted == ("a", "b")
    reference = {(1, 2): "same", (3, 4): "same"}
    arms = {
        "a": {(1, 2): ArmRow(SAME, 0.001, 1.0), (3, 4): ArmRow(DIFF, 0.001, 1.0)},
        "b": {(1, 2): ArmRow(SAME, 0.005, 9.0), (3, 4): ArmRow(SAME, 0.005, 9.0)},
    }
    assert score(rule, arms, reference)["escalation_rate"] == 0.5
