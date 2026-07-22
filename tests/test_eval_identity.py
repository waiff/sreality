"""Pure-metrics tests for the dedup golden-set evaluator (no DB)."""

from __future__ import annotations

from typing import Any

from scripts.eval_identity import _key_from_row, summarize


def _row(sid: int | None, lid: int) -> tuple[Any, ...]:
    # _FEATURES_SQL column order: 0 sreality_id .. 11 obec_id, 12 id (the surrogate).
    return (sid, "sreality", "Nadrazni", 42, "2+kk", "12", 3, 60.0, "desc",
            "prodej", "byt", 500, lid)


def test_key_from_row_uses_surrogate_for_identity() -> None:
    k = _key_from_row(_row(12345, 900001), "id:42")
    assert k.listing_id == 900001
    assert k.property_id == 900001      # the surrogate, NOT the sreality_id
    assert k.sreality_id == 12345


def test_key_from_row_none_safe_sreality_id_post_gate2() -> None:
    # A non-sreality row carries NULL sreality_id once Gate 2 flips; int(None) must not crash.
    k = _key_from_row(_row(None, 900002), "id:42")
    assert k.sreality_id is None
    assert k.property_id == 900002 and k.listing_id == 900002


def test_two_null_sreality_rows_stay_distinct() -> None:
    # Distinct listings must not collapse onto one identity (None == None is True); the
    # surrogate keeps property_id/listing_id distinct so classify_pair never short-circuits
    # a real pair as already_merged / same_listing.
    a = _key_from_row(_row(None, 900003), "id:42")
    b = _key_from_row(_row(None, 900004), "id:42")
    assert a.property_id != b.property_id
    assert a.listing_id != b.listing_id


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
