"""Drawing the sealed exam: the frames, the probabilities, and the door.

What this file CAN prove: the selection policy, the probability arithmetic, the
seal semantics, and the SQL's shape. What it cannot: that probe-by-random-id is
actually uniform — that is a property of Postgres's `random()` over a real id
range, asserted here only as "the statement probes ids and does not TABLESAMPLE".
"""

from __future__ import annotations

from typing import Any

import pytest

from toolkit import tag_exam as te


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    @property
    def rowcount(self) -> int:
        return self._conn.last_rowcount

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        c = self._conn
        c.executed.append((s, params))

        if s.startswith("SELECT count(*)::bigint FROM image_clip_embeddings"):
            self._rows = [(c.frame_size,)]
        elif s.startswith("INSERT INTO tag_exam_cohorts"):
            c.cohort = dict(c.cohort or {}, **{
                "id": 1, "name": params["name"], "frame_size": params["frame_size"],
                "model": params["model"], "revision": params["revision"],
                "drawn_at": "t", "sealed_at": None, "sealed_by": None,
                "note": params["note"],
            })
            self._rows = [tuple(c.cohort[k] for k in te._COHORT_KEYS)]
        elif s.startswith("SELECT id, name, frame_size, model, revision, drawn_at"):
            self._rows = [tuple(c.cohort[k] for k in te._COHORT_KEYS)] if c.cohort else []
        elif s.startswith("WITH bounds AS"):
            self._rows = [(i,) for i in c.probe_hits[: params["count"]]]
        elif s.startswith("SELECT itl.image_id, jsonb_agg"):
            self._rows = [(i, v) for i, v in c.preexisting.items()]
        elif s.startswith("SELECT COALESCE(max(position), 0)::int"):
            self._rows = [(len(c.members),)]
        elif s.startswith("INSERT INTO tag_exam_members"):
            if params["image_id"] in {m["image_id"] for m in c.members}:
                c.last_rowcount = 0
            else:
                c.members.append(dict(params))
                c.last_rowcount = 1
        elif s.startswith("UPDATE tag_exam_cohorts"):
            if c.cohort and c.cohort["sealed_at"] is None:
                c.cohort["sealed_at"] = "sealed-now"
                c.cohort["sealed_by"] = params["sealed_by"]
                self._rows = [(c.cohort["id"], "sealed-now")]
            else:
                self._rows = []
        elif s.startswith("SELECT m.frame, m.stratum"):
            self._rows = list(c.composition_rows)
        else:
            raise AssertionError(f"unhandled SQL: {s}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Txn:
    def __enter__(self) -> "_Txn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self) -> None:
        self.frame_size = 10_360_500
        self.cohort: dict[str, Any] | None = None
        self.probe_hits: list[int] = list(range(1000, 1400))
        self.preexisting: dict[int, Any] = {}
        self.members: list[dict[str, Any]] = []
        self.composition_rows: list[tuple[Any, ...]] = []
        self.executed: list[tuple[str, Any]] = []
        self.last_rowcount = 1

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Txn:
        return _Txn()


@pytest.fixture()
def conn() -> _FakeConn:
    return _FakeConn()


def _open(conn: _FakeConn) -> dict[str, Any]:
    return te.create_cohort(conn, name="exam_v1", model="m", revision="r")


# --- the frame --------------------------------------------------------------


def test_the_frame_size_is_measured_at_creation_and_stored(conn: _FakeConn) -> None:
    # A corpus that grows later must not silently rewrite what a recorded
    # probability meant.
    cohort = _open(conn)
    assert cohort["frame_size"] == 10_360_500


def test_a_cohort_cannot_be_opened_against_an_empty_frame(conn: _FakeConn) -> None:
    conn.frame_size = 0
    with pytest.raises(ValueError, match="nothing to draw from"):
        _open(conn)


def test_the_pure_random_probability_is_n_over_the_frame(conn: _FakeConn) -> None:
    cohort = _open(conn)
    out = te.draw_pure_random(conn, cohort_id=cohort["id"], count=100)
    assert out["inclusion_probability"] == pytest.approx(100 / 10_360_500)
    assert all(m["inclusion_probability"] == out["inclusion_probability"]
               for m in conn.members)


def test_every_pure_random_member_is_in_one_stratum(conn: _FakeConn) -> None:
    cohort = _open(conn)
    te.draw_pure_random(conn, cohort_id=cohort["id"], count=10)
    assert {m["stratum"] for m in conn.members} == {"pure_random"}
    assert {m["frame"] for m in conn.members} == {"pure_random"}


def test_a_short_draw_is_reported_not_padded(conn: _FakeConn) -> None:
    # Padding from another frame would silently turn the incorruptible core into
    # something else; the honest outcome is a smaller core and a loud number.
    conn.probe_hits = [1, 2, 3]
    cohort = _open(conn)
    out = te.draw_pure_random(conn, cohort_id=cohort["id"], count=100)
    assert out["found"] == 3 and out["inserted"] == 3 and out["short_by"] == 97


def test_the_probe_never_asks_for_an_unbounded_number_of_ids(conn: _FakeConn) -> None:
    cohort = _open(conn)
    te.draw_pure_random(conn, cohort_id=cohort["id"], count=10_000)
    probe = next(p for s, p in conn.executed if s.startswith("WITH bounds AS"))
    assert probe["probes"] <= te.PROBE_MAX


# --- how it samples ---------------------------------------------------------


def test_the_pure_random_draw_probes_ids_and_does_not_block_sample() -> None:
    # TABLESAMPLE SYSTEM returns whole 8KB pages, and a page of `images` is mostly
    # ONE listing's photos — a 100-image block sample would be ~25 listings seen
    # four times each. The core exists to be unbiased, so it pays for PK probes.
    sql = " ".join(te._PURE_RANDOM_PROBE_SQL.split())
    assert "TABLESAMPLE" not in sql
    assert "generate_series" in sql and "random()" in sql
    assert "JOIN images i ON i.id = p.id" in sql


def test_the_pure_random_draw_only_takes_showable_embedded_images() -> None:
    sql = " ".join(te._PURE_RANDOM_PROBE_SQL.split())
    assert "i.storage_path IS NOT NULL" in sql   # else it cannot be shown
    assert "image_clip_embeddings" in sql        # else no probe can score it


def test_the_pure_random_draw_never_redraws_an_image_already_under_exam() -> None:
    sql = " ".join(te._PURE_RANDOM_PROBE_SQL.split())
    assert "NOT EXISTS ( SELECT 1 FROM tag_exam_members m WHERE m.image_id = i.id )" in sql


# --- probabilities ----------------------------------------------------------


def test_a_zero_probability_member_is_refused(conn: _FakeConn) -> None:
    # Zero is not a small probability, it is a FILTERED stratum wearing a sample's
    # clothes: 1/p is undefined and the row can never be weighted.
    cohort = _open(conn)
    with pytest.raises(ValueError, match="inclusion_probability"):
        te.add_members(conn, cohort_id=cohort["id"], rows=[
            {"image_id": 1, "frame": "stratified", "stratum": "screen_none",
             "inclusion_probability": 0.0},
        ])


def test_a_probability_above_one_is_refused(conn: _FakeConn) -> None:
    cohort = _open(conn)
    with pytest.raises(ValueError, match="inclusion_probability"):
        te.add_members(conn, cohort_id=cohort["id"], rows=[
            {"image_id": 1, "frame": "stratified", "stratum": "s",
             "inclusion_probability": 1.5},
        ])


def test_an_unknown_frame_is_refused(conn: _FakeConn) -> None:
    cohort = _open(conn)
    with pytest.raises(ValueError, match="unknown frame"):
        te.add_members(conn, cohort_id=cohort["id"], rows=[
            {"image_id": 1, "frame": "convenience", "stratum": "s",
             "inclusion_probability": 0.5},
        ])


def test_nothing_is_written_when_one_row_of_a_batch_is_invalid(
    conn: _FakeConn,
) -> None:
    # Validation runs over the whole batch BEFORE any insert, so a bad row cannot
    # leave the cohort half-written with an unweighted member in it.
    cohort = _open(conn)
    with pytest.raises(ValueError):
        te.add_members(conn, cohort_id=cohort["id"], rows=[
            {"image_id": 1, "frame": "stratified", "stratum": "s",
             "inclusion_probability": 0.5},
            {"image_id": 2, "frame": "stratified", "stratum": "s",
             "inclusion_probability": 0.0},
        ])
    assert conn.members == []


# --- the two-phase draw and the seal ---------------------------------------


def test_an_open_cohort_takes_a_second_write(conn: _FakeConn) -> None:
    # The stratified frame cannot be drawn before the screener runs, so an exam is
    # necessarily two writes. "One-way door" is about SEALING, not about inserts.
    cohort = _open(conn)
    te.draw_pure_random(conn, cohort_id=cohort["id"], count=5)
    added = te.add_members(conn, cohort_id=cohort["id"], rows=[
        {"image_id": 90_001, "frame": "stratified", "stratum": "screen_hit:22",
         "inclusion_probability": 0.4, "screen_guess_tag_ids": [22]},
    ])
    assert added == 1
    assert {m["frame"] for m in conn.members} == {"pure_random", "stratified"}


def test_a_sealed_cohort_refuses_new_members(conn: _FakeConn) -> None:
    cohort = _open(conn)
    te.seal_cohort(conn, cohort_id=cohort["id"])
    with pytest.raises(ValueError, match="sealed"):
        te.add_members(conn, cohort_id=cohort["id"], rows=[
            {"image_id": 7, "frame": "stratified", "stratum": "s",
             "inclusion_probability": 0.5},
        ])


def test_sealing_twice_reports_rather_than_restamping(conn: _FakeConn) -> None:
    # The seal time says which grades were taken against a finished exam; moving
    # it would quietly relabel that history.
    cohort = _open(conn)
    first = te.seal_cohort(conn, cohort_id=cohort["id"])
    second = te.seal_cohort(conn, cohort_id=cohort["id"])
    assert first["status"] == "sealed"
    assert second["status"] == "already_sealed"
    assert second["sealed_at"] == first["sealed_at"]


def test_positions_continue_across_writes(conn: _FakeConn) -> None:
    cohort = _open(conn)
    te.draw_pure_random(conn, cohort_id=cohort["id"], count=3)
    te.add_members(conn, cohort_id=cohort["id"], rows=[
        {"image_id": 90_001, "frame": "stratified", "stratum": "s",
         "inclusion_probability": 0.4},
    ])
    assert [m["position"] for m in conn.members] == [1, 2, 3, 4]


def test_an_unknown_cohort_raises_rather_than_silently_creating_one(
    conn: _FakeConn,
) -> None:
    with pytest.raises(KeyError):
        te.draw_pure_random(conn, cohort_id=999, count=1)


# --- prior state ------------------------------------------------------------


def test_a_preexisting_label_is_frozen_onto_the_member(conn: _FakeConn) -> None:
    # ~0.014% of a pure-random draw lands on an already-labelled image. Rare is not
    # never, and a silently rewritten training label found during analysis is far
    # worse than a column nobody reads.
    conn.probe_hits = [4242]
    conn.preexisting = {4242: [{"tag_id": 22, "state": "positive", "source": "human"}]}
    cohort = _open(conn)
    te.draw_pure_random(conn, cohort_id=cohort["id"], count=1)
    assert conn.members[0]["preexisting_labels"] == [
        {"tag_id": 22, "state": "positive", "source": "human"}
    ]


def test_an_unlabelled_member_carries_no_frozen_state(conn: _FakeConn) -> None:
    cohort = _open(conn)
    te.draw_pure_random(conn, cohort_id=cohort["id"], count=2)
    assert all(m["preexisting_labels"] is None for m in conn.members)
