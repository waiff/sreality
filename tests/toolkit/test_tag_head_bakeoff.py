"""The tagging bake-off orchestrator: selection, free negatives, exam grading,
and what actually gets written.

OFFLINE. Every connection is a fake that answers the handful of statements this
lane may legitimately run and raises on anything else — so a future edit that
reaches for a shortcut over `image_tag_labels` fails here rather than shipping.
Vectors are synthetic and linearly separable; the numbers are not the point, the
shapes and the rules are.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

import pytest

from toolkit import tag_head_bakeoff as bo
from toolkit import tag_heads as th

TRAINED_AT = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)

ENCODER = th.EncoderIdentity(
    model="facebook/dinov3-vitb16", revision="deadbeef", library="transformers",
    pooling="cls", resolution=768, preprocessing="squash", dtype="bfloat16",
)


def _arm(arm_id: int = 7, run_id: int = 3) -> bo.Arm:
    return bo.Arm(id=arm_id, run_id=run_id, arm="dinov3-b16@768/bf16",
                  encoder=ENCODER, dim=8)


# --------------------------------------------------------------- the fake DB

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
        c = self._conn
        c.executed.append((s, params))
        self._rows = c.answer(s, params or {})

    def executemany(self, sql: str, rows: Any) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, list(rows)))
        self._rows = []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeConn:
    """Canned answers for exactly the statements the bake-off may run."""

    def __init__(self, *, tags: dict[int, str], labels: dict[int, dict[int, str]],
                 groups: dict[int, int], vectors: dict[int, list[float]],
                 excluded: dict[int, list[int]] | None = None,
                 run: dict[str, Any] | None = None,
                 arms: list[bo.Arm] | None = None) -> None:
        self.tags = tags
        self.labels = labels                 # {tag_id: {image_id: state}}
        self.groups = groups
        self.vectors = vectors
        self.excluded = excluded or {}
        self.run = run or {"id": 3, "label": "t", "note": None, "status": "running",
                           "manifest_key": None, "heads": [],
                           "min_train_positives": 2}
        self.arms = arms if arms is not None else [_arm()]
        self.executed: list[tuple[str, Any]] = []
        self.written_scores: list[dict[str, Any]] = []
        self.written_metrics: list[dict[str, Any]] = []

    def answer(self, s: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
        if "FROM image_tag_labels l" in s and "l.state = ANY(%(states)s" in s \
                and "l.in_training" in s and "storage_path" not in s:
            tag = int(params["tag_id"])
            want = set(params["states"])
            return sorted((i, st) for i, st in self.labels.get(tag, {}).items()
                          if st in want)
        if "FROM image_tag_labels l" in s and "storage_path" in s:
            tag = int(params["tag_id"])
            if params.get("state") != "excluded" or params.get("offset"):
                return []
            return [(i, "p", "excluded", "human", "pruned", None, None, None,
                     None, None, False) for i in self.excluded.get(tag, [])]
        if "FROM tag_taxonomy WHERE active" in s:
            return sorted(self.tags.items())
        if "FROM tag_exam_cohorts" in s or "FROM tag_exam_sets" in s:
            # No sealed sitting in these fixtures: `load_exam` returns None and
            # the run produces CV numbers only, which is a supported outcome.
            return []
        if "FROM images i" in s:
            want = set(params["image_ids"])
            return [(i, self.groups.get(i)) for i in sorted(want)
                    if i in self.groups or True]
        if "FROM dedup_sim.tag_head_bakeoff_runs" in s and s.startswith("SELECT"):
            r = self.run
            return [(r["id"], TRAINED_AT, r["label"], r["note"], r["status"],
                     r["manifest_key"], r["heads"], r["min_train_positives"])]
        if "FROM dedup_sim.tag_head_bakeoff_arms" in s and s.startswith("SELECT id,"):
            names = params.get("names")
            return [(a.id, a.run_id, a.arm, *[a.encoder.as_dict()[f]
                                              for f in th.ENCODER_FIELDS],
                     a.dim, a.status, a.note)
                    for a in self.arms if not names or a.arm in names]
        if "FROM dedup_sim.tag_head_bakeoff_vectors v" in s:
            want = set(params["image_ids"])
            return [(i, self.vectors[i]) for i in sorted(want) if i in self.vectors]
        if "count(*)::bigint FROM dedup_sim.tag_head_bakeoff_vectors" in s:
            return [(len(self.vectors),)]
        if s.startswith("SELECT m.arm_id, m.mode, m.tag_id"):
            return [(int(m["arm_id"]), m["mode"], int(m["tag_id"]))
                    for m in self.written_metrics]
        if s.startswith("DELETE FROM dedup_sim.tag_head_bakeoff_scores"):
            return []
        if s.startswith("INSERT INTO dedup_sim.tag_head_bakeoff_metrics"):
            self.written_metrics = [
                m for m in self.written_metrics
                if (m["arm_id"], m["mode"], m["tag_id"])
                != (params["arm_id"], params["mode"], params["tag_id"])]
            self.written_metrics.append(dict(params))
            return []
        if s.startswith("UPDATE dedup_sim.tag_head_bakeoff_"):
            return []
        raise AssertionError(f"unexpected SQL: {s[:200]}")

    def cursor(self) -> _Cur:
        return _Cur(self)


def _corpus(*, tags: tuple[int, ...] = (11, 12), n_listings: int = 12,
            per_listing: int = 3, dim: int = 8, seed: int = 5):
    """One image set, labelled for two heads on orthogonal axes so every head is
    separable and no image is positive for both."""
    rnd = random.Random(seed)
    labels: dict[int, dict[int, str]] = {t: {} for t in tags}
    groups: dict[int, int] = {}
    vectors: dict[int, list[float]] = {}
    image_id = 1000
    for listing in range(1, n_listings + 1):
        axis = (listing - 1) % (len(tags) + 1)          # 0..len(tags)
        for _ in range(per_listing):
            image_id += 1
            vec = [rnd.gauss(0.0, 0.15) for _ in range(dim)]
            if axis < len(tags):
                vec[axis] += 1.0
            groups[image_id] = listing
            vectors[image_id] = vec
            for k, tag in enumerate(tags):
                labels[tag][image_id] = "positive" if axis == k else "negative"
    return labels, groups, vectors


def _conn(**kw: Any) -> _FakeConn:
    labels, groups, vectors = _corpus()
    return _FakeConn(tags={11: "kuchyně", 12: "koupelna"}, labels=labels,
                     groups=groups, vectors=vectors, **kw)


# ------------------------------------------------------------ head selection

def test_select_heads_is_a_floor_not_a_list() -> None:
    conn = _conn()
    plans = bo.select_heads(conn, min_train_positives=3)
    assert {p.tag_id for p in plans} == {11, 12}
    assert all(p.selected for p in plans)

    strict = bo.select_heads(conn, min_train_positives=10_000)
    assert not any(p.selected for p in strict)
    # A rejected head is still REPORTED with the count that rejected it.
    assert all("admitted positives" in p.reason for p in strict)
    assert all(p.n_pos > 0 for p in strict)


def test_selection_reads_labels_only_through_the_one_door() -> None:
    conn = _conn()
    bo.select_heads(conn, min_train_positives=1)
    label_reads = [s for s, _ in conn.executed if "image_tag_labels" in s]
    assert label_reads, "sanity: it did read labels"
    for sql in label_reads:
        assert "tag_exam_members hx" in sql, \
            "a label read in this lane lost the sealed-exam exclusion"


# ------------------------------------------------------------ free negatives

def test_free_negatives_exclude_this_heads_own_positives_and_left_outs() -> None:
    positives = {11: {1, 2, 3}, 12: {3, 4, 5}}
    vectors = {i: (float(i), 1.0) for i in range(1, 7)}
    groups = {i: i for i in range(1, 7)}
    rows = bo.free_negatives_for(
        tag_id=11, positives_by_tag=positives, own_excluded={4},
        groups=groups, vectors=vectors)
    # 3 is positive for 11 too; 4 the operator left out for 11. Only 5 is free.
    assert [r.image_id for r in rows] == [5]
    assert all(r.label == 0 for r in rows)


def test_free_negatives_skip_images_the_arm_has_no_vector_for() -> None:
    rows = bo.free_negatives_for(
        tag_id=11, positives_by_tag={11: {1}, 12: {2, 3}}, own_excluded=set(),
        groups={2: 20, 3: 30}, vectors={2: (1.0, 0.0)})
    assert [r.image_id for r in rows] == [2]


def test_free_negatives_give_an_unattributed_image_a_singleton_group() -> None:
    rows = bo.free_negatives_for(
        tag_id=11, positives_by_tag={11: set(), 12: {9}}, own_excluded=set(),
        groups={9: None}, vectors={9: (1.0, 0.0)})
    assert rows[0].listing_id == -9, \
        "a negative group key must never collide with a real listing"


# -------------------------------------------------------------- exam grading

def _sitting(rows: list[dict[str, Any]], tag_ids: tuple[int, ...] = (11,)) -> bo.ExamSitting:
    return bo.ExamSitting(cohort_id=1, set_id=1, tag_ids=tag_ids, rows=tuple(rows))


def _yes_artifact(threshold: float = 0.5) -> dict[str, Any]:
    """A one-dimensional head that says yes iff the vector's first value is high."""
    return {"kind": th.ARTIFACT_KIND, "weights": [10.0], "bias": -5.0,
            "threshold": threshold}


def test_grading_follows_the_ratified_rule_for_every_kind_of_abstention() -> None:
    rows = [
        {"image_id": 1, "picked_tag_ids": [11], "skipped_tag_ids": [],
         "auto_tag_ids": [], "cant_tell": False},                     # human yes
        {"image_id": 2, "picked_tag_ids": [], "skipped_tag_ids": [],
         "auto_tag_ids": [], "cant_tell": False},                     # human no
        {"image_id": 3, "picked_tag_ids": [], "skipped_tag_ids": [11],
         "auto_tag_ids": [], "cant_tell": False},                     # left out
        {"image_id": 4, "picked_tag_ids": [], "skipped_tag_ids": [],
         "auto_tag_ids": [11], "cant_tell": False},                   # declared default
        {"image_id": 5, "picked_tag_ids": [], "skipped_tag_ids": [],
         "auto_tag_ids": [], "cant_tell": True},                      # can't tell
    ]
    vectors = {1: (1.0,), 2: (0.0,), 3: (1.0,), 4: (1.0,), 5: (1.0,)}
    grade = bo.grade_exam(sitting=_sitting(rows), tag_id=11,
                          artifact=_yes_artifact(), threshold=0.5, vectors=vectors)
    assert (grade.tp, grade.fp, grade.tn, grade.fn) == (1, 0, 1, 0)
    assert grade.graded == 2
    # THREE abstentions, and each keeps its photo and its score with a null label.
    assert grade.abstained == 3
    assert sorted(i for i, label, _s, _p in grade.scores if label is None) == [3, 4, 5]


def test_precision_is_none_when_nothing_was_proposed() -> None:
    rows = [{"image_id": 1, "picked_tag_ids": [11], "skipped_tag_ids": [],
             "auto_tag_ids": [], "cant_tell": False}]
    grade = bo.grade_exam(sitting=_sitting(rows), tag_id=11,
                          artifact=_yes_artifact(), threshold=0.5,
                          vectors={1: (0.0,)})
    assert grade.tp == 0 and grade.fn == 1
    assert grade.precision is None, \
        "'nothing was proposed' must not render as 'every proposal was wrong'"
    assert grade.recall == 0.0
    assert grade.f1 is None


def test_images_without_a_vector_are_not_graded_at_all() -> None:
    rows = [{"image_id": 1, "picked_tag_ids": [11], "skipped_tag_ids": [],
             "auto_tag_ids": [], "cant_tell": False}]
    grade = bo.grade_exam(sitting=_sitting(rows), tag_id=11,
                          artifact=_yes_artifact(), threshold=0.5, vectors={})
    assert grade.graded == 0 and grade.abstained == 0 and grade.scores == ()


# ----------------------------------------------------------------- the writes

def test_writing_a_cell_stores_every_score_and_a_metrics_row() -> None:
    pytest.importorskip("sklearn")
    conn = _conn()
    snap = th.assemble_dataset(conn, tag_id=11, encoder=ENCODER,
                               vectors=th.mapping_vector_source(conn.vectors))
    head = th.train_head(snap, trained_at=TRAINED_AT, n_splits=3)
    exam = bo.ExamGrade(tp=2, fp=1, tn=3, fn=0, abstained=4,
                        scores=((1, 1, 0.9, True), (2, None, 0.2, False)))
    outcome = bo.write_head_result(conn, arm_id=7, mode=th.MODE_POS_NEG, tag_id=11,
                                   head=head, exam=exam, trained_at=TRAINED_AT)
    assert outcome.status == "ok"
    assert outcome.n_scores == len(head.oof) + 2

    deletes = [s for s, _ in conn.executed
               if s.startswith("DELETE FROM dedup_sim.tag_head_bakeoff_scores")]
    assert deletes, "a re-run must replace the cell, not layer over it"
    inserts = [rows for s, rows in conn.executed
               if s.startswith("INSERT INTO dedup_sim.tag_head_bakeoff_scores")]
    assert len(inserts) == 1
    written = inserts[0]
    assert {r["split"] for r in written} == {"cv", "exam"}
    assert all(r["label"] is not None for r in written if r["split"] == "cv")
    assert any(r["label"] is None for r in written if r["split"] == "exam")

    metrics = conn.written_metrics[-1]
    assert metrics["exam_graded_n"] == 6 and metrics["exam_abstained_n"] == 4
    assert metrics["dataset_hash"] == snap.dataset_hash
    assert metrics["status"] == "ok"


def test_a_head_that_cannot_be_graded_is_recorded_not_skipped() -> None:
    conn = _conn()
    outcome = bo.write_head_failure(
        conn, arm_id=7, mode=th.MODE_POS_NEG, tag_id=11, n_pos=4, n_neg=9,
        reason="fewer than 2 positive listing-groups", trained_at=TRAINED_AT)
    assert outcome.status == "failed"
    row = conn.written_metrics[-1]
    assert row["status"] == "failed"
    assert row["cv_precision"] is None and row["cv_graded_n"] == 0
    assert "listing-groups" in row["note"]


# --------------------------------------------------------------- end to end

def test_run_bakeoff_covers_the_whole_cross_product_and_resumes() -> None:
    pytest.importorskip("sklearn")
    conn = _conn()
    outcomes = bo.run_bakeoff(conn, run_id=3, min_train_positives=3,
                              n_splits=3, trained_at=TRAINED_AT)
    # 1 arm x 3 modes x 2 heads
    assert len(outcomes) == 6
    assert {(o.mode, o.tag_id) for o in outcomes} == {
        (m, t) for m in th.MODES for t in (11, 12)}
    assert all(o.status == "ok" for o in outcomes), [o.note for o in outcomes]
    assert len(conn.written_metrics) == 6

    # A second pass with the metrics already there writes nothing new.
    again = bo.run_bakeoff(conn, run_id=3, min_train_positives=3, n_splits=3,
                           trained_at=TRAINED_AT)
    assert again == []

    # ...unless asked to redo it.
    forced = bo.run_bakeoff(conn, run_id=3, min_train_positives=3, n_splits=3,
                            force=True, trained_at=TRAINED_AT)
    assert len(forced) == 6


def test_run_bakeoff_without_an_exam_still_produces_cv_numbers() -> None:
    pytest.importorskip("sklearn")
    conn = _conn()
    outcomes = bo.run_bakeoff(conn, run_id=3, min_train_positives=3, n_splits=3,
                              modes=[th.MODE_POS_NEG], trained_at=TRAINED_AT)
    assert all(o.metrics["cv_graded_n"] > 0 for o in outcomes)
    assert all(o.metrics["exam_graded_n"] == 0 for o in outcomes)
    assert all("no sealed exam" in o.note for o in outcomes)


def test_run_bakeoff_refuses_an_unknown_mode() -> None:
    conn = _conn()
    with pytest.raises(bo.BakeoffError, match="unknown mode"):
        bo.run_bakeoff(conn, run_id=3, modes=["pos_only_vibes"])


def test_run_bakeoff_refuses_when_no_head_clears_the_floor() -> None:
    conn = _conn()
    with pytest.raises(bo.BakeoffError, match="admitted training positives"):
        bo.run_bakeoff(conn, run_id=3, min_train_positives=10_000)


# ------------------------------------------------------ the two page readers

class _ReadConn:
    """Canned rows for the page reads, so their SHAPING is what's under test."""

    def __init__(self, answers: dict[str, list[tuple[Any, ...]]]) -> None:
        self.answers = answers
        self.executed: list[tuple[str, Any]] = []

    class _C:
        def __init__(self, outer: "_ReadConn") -> None:
            self.outer = outer
            self.rows: list[tuple[Any, ...]] = []

        def __enter__(self) -> "_ReadConn._C":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def execute(self, sql: str, params: Any = None) -> None:
            s = " ".join(sql.split())
            self.outer.executed.append((s, params))
            for key, rows in self.outer.answers.items():
                if key in s:
                    self.rows = (rows(params) if callable(rows) else rows)
                    return
            self.rows = []

        def fetchall(self) -> list[tuple[Any, ...]]:
            return self.rows

        def fetchone(self) -> tuple[Any, ...] | None:
            return self.rows[0] if self.rows else None

    def cursor(self) -> "_ReadConn._C":
        return _ReadConn._C(self)


def test_run_images_nests_scores_under_each_image_and_pages_on_image_id() -> None:
    conn = _ReadConn({
        "SELECT DISTINCT s.image_id": [(41,), (42,)],
        "FROM images i": [(41, 900, "a.jpg"), (42, None, "b.jpg")],
        "SELECT s.image_id, s.arm_id": [
            (41, 7, "armA", "pos_neg", 11, "kuchyně", "cv", 2, 1, 0.9, True, "tp"),
            (41, 8, "armB", "pos_neg", 11, "kuchyně", "cv", 2, 1, 0.2, False, "fn"),
            (42, 7, "armA", "pos_neg", 11, "kuchyně", "cv", 0, 0, 0.1, False, "tn"),
        ],
    })
    out = bo.run_images(conn, run_id=3, split="cv", limit=2)
    assert [i["image_id"] for i in out["images"]] == [41, 42]
    assert out["images"][0]["storage_path"] == "a.jpg"
    assert len(out["images"][0]["scores"]) == 2
    assert out["images"][1]["listing_id"] is None
    # A full page hands back a cursor; the tiebreaker IS the image id.
    assert out["next_after_image_id"] == 42


def test_run_images_last_page_has_no_cursor() -> None:
    conn = _ReadConn({"SELECT DISTINCT s.image_id": [(41,)],
                      "FROM images i": [(41, 900, "a.jpg")],
                      "SELECT s.image_id, s.arm_id": []})
    out = bo.run_images(conn, run_id=3, split="cv", limit=50)
    assert out["next_after_image_id"] is None


def test_run_images_refuses_an_outcome_without_a_head() -> None:
    with pytest.raises(ValueError, match="outcome needs a tag_id"):
        bo.run_images(_ReadConn({}), run_id=3, outcome="fp")


def test_run_images_refuses_an_unknown_split_or_outcome() -> None:
    with pytest.raises(ValueError, match="split"):
        bo.run_images(_ReadConn({}), run_id=3, split="holdout")
    with pytest.raises(ValueError, match="outcome"):
        bo.run_images(_ReadConn({}), run_id=3, outcome="maybe", tag_id=11)


def test_run_buckets_returns_four_buckets_and_a_labelled_histogram() -> None:
    tiles = [(41, 900, "a.jpg", 0.91, 1, True, 2)]
    conn = _ReadConn({
        "WITH bounds AS": [(1, 1, 3, 0.0, 1.0), (20, 0, 7, 0.0, 1.0),
                           (21, 1, 2, 0.0, 1.0)],
        "AS outcome, count(*)": [("tp", 5), ("fp", 2), ("fn", 1), ("tn", 9),
                                 ("abstained", 4)],
        "JOIN images i ON i.id = s.image_id": tiles,
    })
    out = bo.run_buckets(conn, arm_id=7, mode="pos_neg", tag_id=11, split="cv")
    assert set(out["buckets"]) == set(bo.OUTCOMES)
    assert out["buckets"]["tp"]["count"] == 5
    assert out["buckets"]["fp"]["tiles"][0]["image_id"] == 41
    # Abstained cells are reported BESIDE the four, never folded into one.
    assert out["abstained_count"] == 4
    hist = out["histogram"]
    assert hist["bins"] == 20 and len(hist["positive"]) == 20
    assert hist["positive"][0] == 3
    assert hist["negative"][19] == 7
    # width_bucket's overflow bin (21) belongs in the last bin, not off the end.
    assert hist["positive"][19] == 2


def test_run_buckets_refuses_an_unknown_split() -> None:
    with pytest.raises(ValueError, match="split"):
        bo.run_buckets(_ReadConn({}), arm_id=7, mode="pos_neg", tag_id=11,
                       split="both")


def test_run_metrics_types_every_column_and_isoformats_the_timestamp() -> None:
    row = (7, "armA", "pos_neg", 11, "kuchyně", 231, 1004, 612,
           0.94, 0.89, 0.915, 1235, 206, 13, 991, 25,
           None, None, None, 0, 250, 0, 0, 0, 0,
           0.5, "9f2c", "ok", None, TRAINED_AT)
    conn = _ReadConn({"FROM dedup_sim.tag_head_bakeoff_metrics m": [row]})
    out = bo.run_metrics(conn, run_id=3)[0]
    assert out["arm"] == "armA" and out["tag_label"] == "kuchyně"
    assert out["cv_precision"] == 0.94 and out["exam_precision"] is None
    assert out["trained_at"] == TRAINED_AT.isoformat()


def test_run_bakeoff_opens_no_label_door_of_its_own() -> None:
    pytest.importorskip("sklearn")
    conn = _conn()
    bo.run_bakeoff(conn, run_id=3, min_train_positives=3, n_splits=3,
                   modes=[th.MODE_POS_NEG], trained_at=TRAINED_AT)
    for sql, _ in conn.executed:
        if "image_tag_labels" in sql:
            assert "tag_exam_members hx" in sql, \
                f"a label read lost the sealed-exam exclusion: {sql[:120]}"
