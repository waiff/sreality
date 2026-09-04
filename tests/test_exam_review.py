"""The exam review subpage's read: a sitting's answers served back for
correction. The property that matters is the round trip — what `answers()`
serves must be exactly what `record_answer` wrote, in record_answer's own
vocabulary, because the review page POSTs corrections straight back through it.
"""

from __future__ import annotations

from typing import Any


class _Cur:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *a: Any) -> None: ...

    def execute(self, sql: str, params: Any = None) -> None:
        self.params = params

    def fetchall(self) -> list[tuple]:
        return self._rows


class _Conn:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def cursor(self) -> _Cur:
        return _Cur(self._rows)


def _cells(**by_tag: dict) -> dict:
    return {str(k): v for k, v in by_tag.items()}


def test_answers_speak_record_answers_vocabulary() -> None:
    # positive -> picked, excluded/'pruned' -> skipped, negative -> untouched.
    from toolkit import tag_exam

    rows = [(555, 13, {
        "22": {"state": "positive", "reason": None},
        "25": {"state": "excluded", "reason": "pruned"},
        "46": {"state": "negative", "reason": None},
    })]
    out = tag_exam.answers(_Conn(rows), cohort_id=1, tag_ids=[22, 25, 46])
    assert out == [{
        "image_id": 555, "position": 13,
        "picked_tag_ids": [22], "skipped_tag_ids": [25],
        "auto_tag_ids": [], "cant_tell": False, "suggested_tag_ids": None,
    }]


def test_cant_tell_is_only_the_full_ambiguous_sweep() -> None:
    # record_answer writes excluded/'ambiguous' on EVERY tag or on none, so a
    # full sweep is the only honest cant_tell. A single ambiguous cell (older
    # data, or a future partial writer) renders as its per-tag states instead of
    # silently claiming the whole image was undecidable.
    from toolkit import tag_exam

    full = [(1, 1, {
        "22": {"state": "excluded", "reason": "ambiguous"},
        "25": {"state": "excluded", "reason": "ambiguous"},
    })]
    out = tag_exam.answers(_Conn(full), cohort_id=1, tag_ids=[22, 25])
    assert out[0]["cant_tell"] is True
    assert out[0]["picked_tag_ids"] == [] and out[0]["skipped_tag_ids"] == []

    partial = [(1, 1, {
        "22": {"state": "excluded", "reason": "ambiguous"},
        "25": {"state": "positive", "reason": None},
    })]
    out = tag_exam.answers(_Conn(partial), cohort_id=1, tag_ids=[22, 25])
    assert out[0]["cant_tell"] is False
    assert out[0]["picked_tag_ids"] == [25]


def test_only_fully_answered_images_qualify() -> None:
    # The SQL's HAVING mirrors _PROGRESS_SQL: a half-answered image belongs to
    # the exam screen, not review. Source-level pin, since the fake conn cannot
    # execute HAVING.
    from toolkit import tag_exam

    sql = " ".join(tag_exam._ANSWERS_SQL.split())
    assert "HAVING count(*) = %(tag_count)s" in sql
    assert "ORDER BY m.position" in sql
    assert "l.source IN ('human', 'human_confirmed')" in sql


def test_a_backfilled_default_is_flagged_not_hidden() -> None:
    # 466's bulk negatives are declared defaults; the review page fences them
    # until the operator re-answers, and that fence is driven by created_by.
    from toolkit import tag_exam

    # 'auto' is computed IN SQL: marker AND untouched since insert. created_by
    # is insert-only, so a re-answered default must stop being auto — the first
    # version keyed on the marker alone and fenced every row forever.
    rows = [(1, 1, {
        "22": {"state": "positive", "reason": None, "auto": False},
        "25": {"state": "negative", "reason": None, "auto": True},
    })]
    out = tag_exam.answers(_Conn(rows), cohort_id=1, tag_ids=[22, 25])
    assert out[0]["auto_tag_ids"] == [25]
    assert out[0]["picked_tag_ids"] == [22]


def test_auto_means_marker_and_untouched_since_insert() -> None:
    from toolkit import tag_exam
    sql = " ".join(tag_exam._ANSWERS_SQL.split())
    assert "l.created_by = 'backfill:466'" in sql
    assert "l.updated_at <= l.created_at" in sql


def test_suggestions_ride_along_when_the_set_is_known() -> None:
    from toolkit import tag_exam

    class _Cur:
        def __init__(self, owner): self.o = owner
        def __enter__(self): return self
        def __exit__(self, *a): ...
        def execute(self, sql, params=None): self._sql = " ".join(sql.split())
        def fetchall(self):
            if "tag_exam_suggestions" in self._sql:
                return [(555, [22, 99])]           # 99 is not asked any more
            return [(555, 1, {"22": {"state": "negative", "reason": None, "auto": False}})]

    class _Conn:
        def cursor(self): return _Cur(self)

    out = tag_exam.answers(_Conn(), cohort_id=1, tag_ids=[22], set_id=1)
    assert out[0]["suggested_tag_ids"] == [22]     # filtered to the current list
