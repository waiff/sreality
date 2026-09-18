"""The D6 arithmetic, checked by hand.

This module decides whether the program continues (gold-vs-operator ≥ 0.95), so every number
it produces is pinned against a value computed outside the code: the Wilson bounds below are
the textbook formula evaluated independently, not this function's own output frozen.
"""

from __future__ import annotations

from math import sqrt

import pytest

from autodedup.agreement import summarize, wilson_interval


def _row(
    lo: int,
    hi: int,
    operator: str,
    judge: str,
    tier: str = "gold",
    source: str = "explicit",
) -> dict[str, object]:
    return {
        "listing_lo": lo,
        "listing_hi": hi,
        "operator_verdict": operator,
        "operator_source": source,
        "judge_verdict": judge,
        "judge_tier": tier,
        "judge_model": "gpt-5.6-luna",
    }


# ------------------------------------------------------------------------ the interval


def test_wilson_matches_the_formula_evaluated_by_hand():
    """19 of 20, z = 1.959964. Computed here from the formula, in the open, so a rewrite of
    the implementation cannot quietly redefine the number the gate is read against."""
    k, n, z = 19, 20, 1.959963984540054
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    low, high = wilson_interval(k, n)
    assert low == pytest.approx(centre - half)
    assert high == pytest.approx(centre + half)
    # And the shape that makes Wilson the right choice here: near 1.0 it does not run past it.
    assert 0.0 <= low <= high <= 1.0
    assert high < 1.0


def test_a_perfect_run_keeps_a_lower_bound_below_one():
    """10 of 10 is not proof of 1.0, and the interval says so: the upper bound sits at 1 and
    the lower one well under it — the number an operator needs to read off a small sample."""
    low, high = wilson_interval(10, 10)
    assert high == pytest.approx(1.0)
    assert low == pytest.approx(0.72246720, abs=1e-6)


def test_no_trial_is_no_interval_rather_than_zero():
    assert wilson_interval(0, 0) == (None, None)


# ------------------------------------------------------------------- the binary mapping


def test_the_three_negative_operator_verdicts_all_mean_not_one_property():
    rows = [
        _row(1, 2, "different", "different_property"),
        _row(3, 4, "same_building_different_unit", "different_property"),
        # The judge's OWN `same_building_different_unit` is also "not one property".
        _row(5, 6, "same_project_different_unit", "same_building_different_unit"),
        _row(7, 8, "same", "same_property"),
    ]
    out = summarize(rows)
    assert out["overall"]["n"] == 4
    assert out["overall"]["n_agree"] == 4
    assert out["overall"]["agreement"] == 1.0


def test_the_two_directions_of_disagreement_are_reported_apart():
    rows = [
        _row(1, 2, "same", "different_property"),  # judge under-called a duplicate
        _row(3, 4, "different", "same_property"),  # judge over-called one
        _row(5, 6, "same", "same_property"),
    ]
    out = summarize(rows)["overall"]
    assert out["n"] == 3
    assert out["n_agree"] == 1
    assert out["n_judge_different_operator_same"] == 1
    assert out["n_judge_same_operator_different"] == 1


def test_insufficient_evidence_is_counted_and_never_scored():
    rows = [
        _row(1, 2, "same", "insufficient_evidence", tier="vision"),
        _row(3, 4, "same", "same_property"),
    ]
    out = summarize(rows)
    assert out["overall"]["n"] == 1
    assert out["overall"]["agreement"] == 1.0
    assert out["n_insufficient_evidence"] == 1
    assert out["insufficient_by_tier"] == {"vision": 1}


def test_every_tier_is_reported_even_with_no_pair():
    out = summarize([_row(1, 2, "same", "same_property", tier="gold")])
    assert [t["tier"] for t in out["tiers"]] == ["gold", "vision", "text"]
    empty = next(t for t in out["tiers"] if t["tier"] == "text")
    assert empty["n"] == 0
    # "not measured" is null, never a zero agreement.
    assert empty["agreement"] is None
    assert empty["ci_low"] is None


def test_explicit_and_implied_labels_are_counted_apart():
    rows = [
        _row(1, 2, "same", "same_property", source="implied"),
        _row(3, 4, "same", "same_property", source="implied"),
        _row(5, 6, "different", "different_property", source="explicit"),
    ]
    out = summarize(rows)["overall"]
    assert (out["n_explicit"], out["n_implied"]) == (1, 2)


def test_the_disagreement_table_is_capped_and_still_counts_the_rest():
    rows = [_row(i, i + 1000, "same", "different_property") for i in range(1, 81)]
    out = summarize(rows)
    assert out["n_disagreements"] == 80
    assert len(out["disagreements"]) == 50
    # The rows carry what a reader needs to open the pair, and nothing internal.
    assert set(out["disagreements"][0]) == {
        "listing_lo",
        "listing_hi",
        "operator_verdict",
        "operator_source",
        "judge_verdict",
        "judge_tier",
        "judge_model",
    }


def test_the_gate_travels_with_the_numbers():
    out = summarize([])
    assert out["gate"] == {"tier": "gold", "bar": 0.95, "target_n": 200}
    assert out["overall"]["n"] == 0
    assert out["overall"]["agreement"] is None
