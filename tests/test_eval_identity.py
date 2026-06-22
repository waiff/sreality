"""Pure-metrics tests for the dedup golden-set evaluator (no DB)."""

from __future__ import annotations

from scripts.eval_identity import summarize


def test_summarize_counts_and_metrics() -> None:
    obs = [
        (True, "auto_merge", "byt"),    # true positive auto
        (True, "candidate", "byt"),     # found but not auto
        (True, "reject", "byt"),        # missed
        (False, "reject", "byt"),       # true negative
        (False, "auto_merge", "byt"),   # FALSE MERGE
    ]
    g = summarize(obs)
    byt = g["byt"]
    assert byt["positives"] == 3
    assert byt["negatives"] == 2
    assert byt["tp_auto"] == 1
    assert byt["fp_auto"] == 1
    assert byt["precision_auto"] == 0.5            # 1 / (1 + 1)
    assert byt["recall_auto"] == round(1 / 3, 4)   # 1 / 3 positives
    assert byt["recall_found"] == round(2 / 3, 4)  # auto + candidate
    assert byt["false_merge_rate"] == 0.5          # 1 / 2 negatives
    assert byt["pred"]["auto_merge"] == 2


def test_summarize_rolls_up_all_and_splits_by_category() -> None:
    obs = [
        (True, "auto_merge", "byt"),
        (True, "not_blocked", "dum"),
        (False, "reject", "dum"),
    ]
    g = summarize(obs)
    assert set(g) == {"byt", "dum", "__all__"}
    assert g["__all__"]["positives"] == 2
    assert g["__all__"]["negatives"] == 1
    # dum: the coverage gap — a true pair is never even blocked, recall 0.
    assert g["dum"]["recall_found"] == 0.0
    assert g["dum"]["recall_auto"] == 0.0
    # No negatives auto-merged anywhere → false_merge_rate 0 where negatives exist.
    assert g["dum"]["false_merge_rate"] == 0.0


def test_summarize_none_metrics_when_no_denominator() -> None:
    # All positives, no negatives → precision_auto / false_merge_rate undefined (None).
    g = summarize([(True, "candidate", "komercni")])
    assert g["komercni"]["precision_auto"] is None
    assert g["komercni"]["false_merge_rate"] is None
    assert g["komercni"]["recall_found"] == 1.0
