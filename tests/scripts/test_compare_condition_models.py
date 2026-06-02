"""Hermetic tests for the pure agreement math in compare_condition_models.

No DB, no LLM — the psycopg/api imports live inside main(), so importing
the module and calling summarize_agreement is side-effect free.
"""

from __future__ import annotations

from scripts.compare_condition_models import _axis_stats, summarize_agreement


def test_axis_stats_perfect_agreement():
    s = _axis_stats([(3, 3), (5, 5), (1, 1)])
    assert s["n"] == 3
    assert s["exact_pct"] == 100.0
    assert s["within1_pct"] == 100.0
    assert s["mean_abs_diff"] == 0.0
    assert s["bias"] == 0.0


def test_axis_stats_counts_within_one_and_bias():
    # diffs: +1, +1, -1  -> exact 0, all within 1, bias +1/3
    s = _axis_stats([(3, 4), (2, 3), (5, 4)])
    assert s["exact"] == 0
    assert s["within1_pct"] == 100.0
    assert s["mean_abs_diff"] == 1.0
    assert s["bias"] == round(1 / 3, 3)


def test_axis_stats_hard_disagreement_drops_within1():
    s = _axis_stats([(1, 5), (2, 2)])  # diffs +4, 0
    assert s["exact_pct"] == 50.0
    assert s["within1_pct"] == 50.0
    assert s["mean_abs_diff"] == 2.0
    assert s["bias"] == 2.0


def test_axis_stats_empty():
    assert _axis_stats([]) == {"n": 0}


def test_summarize_agreement_splits_axes():
    rows = [(3, 3, 4, 5), (2, 2, 1, 1)]
    out = summarize_agreement(rows)
    assert out["n"] == 2
    # building exact on both rows; apartment exact on one
    assert out["building"]["exact"] == 2
    assert out["apartment"]["exact"] == 1
