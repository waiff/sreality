"""The sealed exam's membership store and the one door training reads through."""

from __future__ import annotations

from typing import Any

import pytest

from toolkit import tag_holdout as th


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if s.startswith("SELECT itl.image_id, itl.state"):
            self._rows = list(self._conn.training_rows)
        elif s.startswith("SELECT id, name, frame_size"):
            self._rows = list(self._conn.cohorts)
        elif s.startswith("SELECT count(*)::int FROM tag_exam_members WHERE cohort_id"):
            self._rows = [(self._conn.cohort_member_count,)]
        elif s.startswith("SELECT count(*)::int FROM tag_exam_members"):
            self._rows = [(self._conn.total_members,)]
        else:
            raise AssertionError(f"unhandled SQL: {s}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeConn:
    def __init__(self) -> None:
        self.training_rows: list[tuple[Any, ...]] = [(1, "positive"), (2, "negative")]
        self.cohorts: list[tuple[Any, ...]] = []
        self.cohort_member_count = 0
        self.total_members = 0
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)


@pytest.fixture()
def conn() -> _FakeConn:
    return _FakeConn()


# --- the exclusion ----------------------------------------------------------


def test_the_exclusion_binds_to_the_callers_own_alias() -> None:
    # Every statement names its label row differently; one shared constant with a
    # hard-coded alias would silently match the wrong table in half of them.
    assert "hx.image_id = itl.image_id" in th.exclusion_for("itl")
    assert "hx.image_id = p.image_id" in th.exclusion_for("p")


def test_membership_alone_excludes_regardless_of_sealing() -> None:
    # Protecting only SEALED cohorts leaves the whole drawing window open — exactly
    # when images are chosen but not yet answered. A training run inside that window
    # would consume the exam before the exam existed.
    assert "sealed_at" not in th.HOLDOUT_EXCLUSION


def test_the_exclusion_is_an_and_fragment_not_a_statement() -> None:
    # It is formatted INTO a WHERE clause; a leading WHERE would produce a syntax
    # error in every caller that already has one.
    assert th.HOLDOUT_EXCLUSION.strip().startswith("AND NOT EXISTS")


# --- the sanctioned training door -------------------------------------------


def test_training_rows_carry_the_exclusion(conn: _FakeConn) -> None:
    th.training_label_rows(conn, tag_id=22)
    sql = conn.executed[0][0]
    assert "tag_exam_members" in sql


def test_training_rows_are_human_only(conn: _FakeConn) -> None:
    # A machine label is a proposal, not ground truth; training on unreviewed
    # machine output would close the loop on the model's own errors.
    th.training_label_rows(conn, tag_id=22)
    assert "itl.source IN ('human', 'human_confirmed')" in conn.executed[0][0]


def test_excluded_is_not_a_trainable_state(conn: _FakeConn) -> None:
    # An image nobody could decide is not a negative. Feeding it as one teaches the
    # probe the operator's uncertainty as if it were a fact.
    assert "excluded" not in th.TRAINING_STATES
    with pytest.raises(ValueError, match="not trainable"):
        th.training_label_rows(conn, tag_id=22, states=("positive", "excluded"))


def test_training_rows_returns_typed_pairs(conn: _FakeConn) -> None:
    assert th.training_label_rows(conn, tag_id=22) == [(1, "positive"), (2, "negative")]


def test_training_rows_binds_the_tag(conn: _FakeConn) -> None:
    th.training_label_rows(conn, tag_id=22)
    assert conn.executed[0][1]["tag_id"] == 22


# --- cohorts ----------------------------------------------------------------


def test_an_unknown_cohort_is_none_not_an_error(conn: _FakeConn) -> None:
    assert th.get_cohort(conn, name="exam_v1") is None


def test_a_cohort_reads_back_as_a_named_mapping(conn: _FakeConn) -> None:
    conn.cohorts = [(1, "exam_v1", 10_400_000, "m", "abc", "t0", None, None, None)]
    got = th.get_cohort(conn, name="exam_v1")
    assert got is not None
    assert got["name"] == "exam_v1"
    assert got["frame_size"] == 10_400_000
    # sealed_at NULL means "still being drawn", never "unprotected".
    assert got["sealed_at"] is None


def test_holdout_size_counts_every_cohort(conn: _FakeConn) -> None:
    # What the training reads are excluding, across all exams — the number to sanity
    # check against the exam's own size.
    conn.total_members = 250
    assert th.holdout_size(conn) == 250


def test_cohort_size_is_scoped_to_one_cohort(conn: _FakeConn) -> None:
    conn.cohort_member_count = 100
    assert th.cohort_size(conn, cohort_id=1) == 100
    assert conn.executed[-1][1] == {"cohort_id": 1}
