"""The accuracy gate: machine verdicts scored against the human exam answers.

The rule under test is the operator's ratified one — a cell grades ONLY when
both sides committed to yes or no. Scoring an abstention as a negative is the
tempting shortcut and it is wrong twice over: it punishes the model for obeying
the leave-out rule, and it inflates the denominator with cells that train
nothing.
"""

from __future__ import annotations

from typing import Any

TAGS = [22, 25, 36]


def _row(image_id: int, picked: list[int], skipped: list[int] | None = None,
         cant_tell: bool = False) -> dict[str, Any]:
    return {"image_id": image_id, "position": image_id,
            "picked_tag_ids": picked, "skipped_tag_ids": skipped or [],
            "cant_tell": cant_tell}


def _review(**verdicts: str) -> dict[str, Any]:
    return {"verdicts": {int(k[1:]): v for k, v in verdicts.items()},
            "dismissed_tag_ids": [], "reviewed_at": "t"}


def test_the_four_confusion_cells() -> None:
    from toolkit import exam_machine_review as mr

    out = mr.agreement(
        rows=[_row(1, [22]), _row(2, [])],
        reviews={1: _review(t22="yes", t25="yes", t36="no"),
                 2: _review(t22="no", t25="no", t36="no")},
        tag_ids=TAGS)
    assert out[22]["tp"] == 1 and out[22]["tn"] == 1          # agreed both ways
    assert out[25]["fp"] == 1 and out[25]["tn"] == 1          # machine over-called
    assert out[36]["tn"] == 2


def test_a_human_positive_the_machine_missed_is_a_false_negative() -> None:
    from toolkit import exam_machine_review as mr

    out = mr.agreement(rows=[_row(1, [22])], reviews={1: _review(
        t22="no", t25="no", t36="no")}, tag_ids=TAGS)
    assert out[22]["fn"] == 1 and out[22]["tp"] == 0


def test_a_left_out_on_either_side_grades_nothing() -> None:
    from toolkit import exam_machine_review as mr

    out = mr.agreement(
        rows=[_row(1, [], skipped=[22]), _row(2, [25])],
        reviews={1: _review(t22="yes", t25="no", t36="no"),
                 2: _review(t22="no", t25="skip", t36="no")},
        tag_ids=TAGS)
    # Image 1: the human left 22 out. Not a false positive — an abstention.
    assert out[22]["human_skip"] == 1
    assert out[22]["fp"] == 0 and out[22]["tn"] == 1
    # Image 2: the machine left 25 out against a human yes. Also not graded.
    assert out[25]["machine_skip"] == 1
    assert out[25]["fn"] == 0 and out[25]["tp"] == 0


def test_a_cant_tell_row_abstains_on_every_head() -> None:
    from toolkit import exam_machine_review as mr

    out = mr.agreement(rows=[_row(1, [], cant_tell=True)],
                       reviews={1: _review(t22="yes", t25="no", t36="no")},
                       tag_ids=TAGS)
    assert [out[t]["human_skip"] for t in TAGS] == [1, 1, 1]
    assert sum(out[t]["tp"] + out[t]["fp"] + out[t]["fn"] + out[t]["tn"]
               for t in TAGS) == 0


def test_an_unreviewed_image_is_counted_apart_never_as_agreement() -> None:
    from toolkit import exam_machine_review as mr

    out = mr.agreement(rows=[_row(1, [22]), _row(2, [22])],
                       reviews={1: _review(t22="yes", t25="no", t36="no")},
                       tag_ids=TAGS)
    assert out[22]["unreviewed"] == 1 and out[22]["tp"] == 1
    # A verdict missing for ONE head of a reviewed image is equally unreviewed.
    out2 = mr.agreement(rows=[_row(1, [22])],
                        reviews={1: _review(t22="yes", t25="no")}, tag_ids=TAGS)
    assert out2[36]["unreviewed"] == 1


def test_scored_reports_no_proposals_as_none_not_zero() -> None:
    # "Nothing was ever proposed" and "every proposal was wrong" are opposite
    # facts; rendering both as 0.00 would hide a silent head.
    from toolkit import exam_machine_review as mr

    empty = mr.scored({"tp": 0, "fp": 0, "fn": 0, "tn": 9,
                       "human_skip": 0, "machine_skip": 0, "unreviewed": 0})
    assert empty["precision"] is None and empty["recall"] is None
    assert empty["graded"] == 9 and empty["human_positives"] == 0

    real = mr.scored({"tp": 3, "fp": 1, "fn": 1, "tn": 5,
                      "human_skip": 2, "machine_skip": 0, "unreviewed": 0})
    assert real["precision"] == 0.75 and real["recall"] == 0.75
    assert real["graded"] == 10 and real["human_positives"] == 4


def test_the_script_reads_only() -> None:
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "scripts" / "exam_agreement.py").read_text()
    for forbidden in ("INSERT", "UPDATE ", "DELETE", "run_vision_batch", "record_"):
        assert forbidden not in src, forbidden


def test_an_untouched_backfill_default_grades_nothing() -> None:
    # Migration 466 declared ten heads negative on exam_v1 rather than re-sit
    # 250 images. A cell the operator has not touched since is a DEFAULT, not a
    # judgment: grading the machine against it measures the backfill, and it
    # reads as a false positive exactly when the machine is right.
    from toolkit import exam_machine_review as mr

    rows = [{**_row(1, []), "auto_tag_ids": [25, 36]}]
    out = mr.agreement(rows=rows, reviews={1: _review(t22="no", t25="yes", t36="no")},
                       tag_ids=TAGS)
    assert out[25]["fp"] == 0 and out[25]["auto_default"] == 1
    assert out[36]["tn"] == 0 and out[36]["auto_default"] == 1
    # A head with no backfill on it still grades normally.
    assert out[22]["tn"] == 1 and out[22]["auto_default"] == 0


def test_a_default_the_operator_has_since_answered_grades_again() -> None:
    # answers() drops the auto marker on re-answer, so the cell simply becomes
    # a judgment — no special case needed here beyond honouring the marker.
    from toolkit import exam_machine_review as mr

    out = mr.agreement(rows=[{**_row(1, [25]), "auto_tag_ids": []}],
                       reviews={1: _review(t22="no", t25="yes", t36="no")},
                       tag_ids=TAGS)
    assert out[25]["tp"] == 1 and out[25]["auto_default"] == 0


def test_the_two_abstention_kinds_are_counted_apart() -> None:
    from toolkit import exam_machine_review as mr

    rows = [{**_row(1, [], skipped=[22]), "auto_tag_ids": [25]}]
    out = mr.agreement(rows=rows, reviews={1: _review(t22="yes", t25="yes", t36="no")},
                       tag_ids=TAGS)
    assert out[22]["human_skip"] == 1 and out[22]["auto_default"] == 0
    assert out[25]["auto_default"] == 1 and out[25]["human_skip"] == 0
