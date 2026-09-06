"""Per-tag binary heads: assembly, the holdout's survival, grouping, the artifact.

NOTHING HERE TOUCHES A DATABASE OR A REAL LABEL. Every row is synthetic and every
connection is a fake. That is not incidental to the tests — the operator's
training set is not finalized, and a trainer that reads it before it is finalized
(or that reads the sealed exam at all) is the failure this whole lane is shaped
to prevent. The tests that matter most below are the ones asserting this module
opens no door of its own.
"""

from __future__ import annotations

import json
import math
import random
from datetime import datetime, timezone
from typing import Any

import pytest

from toolkit import tag_heads as th

MARKER = ("NOT EXISTS ( SELECT 1 FROM tag_exam_members hx "
          "JOIN tag_exam_cohorts hc")

ENCODER = th.EncoderIdentity(
    model="facebook/dinov3-vitb16", revision="deadbeef", library="transformers",
    pooling="cls", resolution=224, preprocessing="resize-shortest-224-centercrop",
    dtype="float32",
)
TRAINED_AT = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)


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
        if "FROM image_tag_labels l" in s and "row_number() OVER" in s:
            c.doors_used.append("set_positives")
            self._rows = [(i,) for i in c.positive_ids]
        elif s.startswith("SELECT itl.image_id, itl.state"):
            c.doors_used.append("training_label_rows")
            self._rows = [(i, "negative") for i in c.negative_ids]
        elif "FROM image_dinov3_embeddings" in s and "GROUP BY" in s:
            facts = c.encoder.as_dict()
            self._rows = [tuple(facts[f] for f in th.ENCODER_FIELDS) + (99,)]
        elif "FROM image_dinov3_embeddings e" in s:
            if params["model"] != c.encoder.model:
                self._rows = []
            else:
                want = set(params["image_ids"])
                self._rows = [(i, c.render(v)) for i, v in sorted(c.embeddings.items())
                              if i in want]
        elif "FROM images i" in s:
            want = set(params["image_ids"])
            self._rows = [(i, g) for i, g in sorted(c.groups.items()) if i in want]
        else:
            raise AssertionError(f"unexpected SQL: {s[:160]}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeConn:
    """Canned rows for exactly the five statements this lane can legitimately run."""

    def __init__(self, *, positive_ids, negative_ids, embeddings, groups,
                 encoder=ENCODER, as_text: bool = True) -> None:
        self.positive_ids = list(positive_ids)
        self.negative_ids = list(negative_ids)
        self.embeddings = dict(embeddings)
        self.groups = dict(groups)
        self.encoder = encoder
        self.as_text = as_text
        self.executed: list[tuple[str, Any]] = []
        self.doors_used: list[str] = []

    def render(self, vec) -> Any:
        # pgvector hands back '[...]' text unless an adapter is registered; both
        # shapes must parse.
        return "[" + ",".join(repr(float(x)) for x in vec) + "]" if self.as_text \
            else list(vec)

    def cursor(self) -> _Cur:
        return _Cur(self)


def _make_corpus(*, dim: int = 16, n_listings: int = 24, per_listing: int = 3,
                 seed: int = 7, spread: float = 0.25):
    """Linearly separable synthetic data with PURE listing groups.

    Positive listings cluster near +axis0, negatives near -axis0, every image of a
    listing shares its label. Pure groups are what makes the leakage question
    answerable: if a split ever put one listing on both sides, the head would be
    scoring on photos of a listing it trained on.
    """
    rnd = random.Random(seed)
    pos_ids, neg_ids, embeddings, groups = [], [], {}, {}
    image_id = 1000
    for listing in range(1, n_listings + 1):
        positive = listing % 2 == 0
        for _ in range(per_listing):
            image_id += 1
            centre = [1.0 if k == 0 else 0.0 for k in range(dim)]
            if not positive:
                centre[0] = -1.0
            embeddings[image_id] = [c + rnd.gauss(0.0, spread) for c in centre]
            groups[image_id] = listing
            (pos_ids if positive else neg_ids).append(image_id)
    return pos_ids, neg_ids, embeddings, groups


def _conn(**kw) -> _FakeConn:
    pos, neg, emb, grp = _make_corpus(**{k: v for k, v in kw.items()
                                         if k in {"dim", "n_listings", "per_listing",
                                                  "seed", "spread"}})
    return _FakeConn(positive_ids=pos, negative_ids=neg, embeddings=emb, groups=grp,
                     as_text=kw.get("as_text", True))


# ------------------------------------------------------- the holdout survives

def test_assembly_reads_labels_only_through_the_two_sanctioned_doors() -> None:
    # With the REAL doors running against the fake, every statement that names
    # image_tag_labels is one of theirs — and carries the exclusion. If a future
    # edit adds a "let me just double-check the labels" query, this fails.
    conn = _conn()
    th.assemble_dataset(conn, tag_id=19)

    label_reads = [s for s, _ in conn.executed if "image_tag_labels" in s]
    assert len(label_reads) == 2, label_reads
    for sql in label_reads:
        assert MARKER in sql, "a label read lost the sealed-exam exclusion"
    assert sorted(conn.doors_used) == ["set_positives", "training_label_rows"]


def test_assembly_adds_no_back_door_when_the_doors_are_stubbed(monkeypatch) -> None:
    # The doors are what exclude the exam. Replace them with stubs that hand back a
    # mix INCLUDING an image the exam would have hidden, and the assembly must
    # still issue no label SQL of its own — no "enrich", no "double-check".
    holdout_image = 4242
    seen: dict[str, Any] = {}

    def fake_positives(conn, *, tag_id, default_target=300):
        seen["positives"] = {"tag_id": tag_id, "default_target": default_target}
        return [1001, 1002, holdout_image]

    def fake_negatives(conn, *, tag_id, states=("positive", "negative"),
                       include_holdout=False):
        seen["negatives"] = {"tag_id": tag_id, "states": states,
                             "include_holdout": include_holdout}
        return [(1003, "negative"), (1004, "negative")]

    monkeypatch.setattr(th.machine_labeling, "training_set_positive_ids", fake_positives)
    monkeypatch.setattr(th.tag_holdout, "training_label_rows", fake_negatives)

    ids = [1001, 1002, 1003, 1004, holdout_image]
    conn = _FakeConn(positive_ids=[], negative_ids=[],
                     embeddings={i: [float(i), 1.0] for i in ids},
                     groups={i: i for i in ids})
    snap = th.assemble_dataset(conn, tag_id=19)

    assert not [s for s, _ in conn.executed if "image_tag_labels" in s], \
        "assemble_dataset queried the label table itself — that is the back door"
    # And it never opens the operator's own exam door.
    assert seen["negatives"]["include_holdout"] is False
    assert seen["negatives"]["states"] == ("negative",)
    assert seen["positives"]["tag_id"] == 19
    assert {r.image_id for r in snap.rows} == set(ids)


def test_no_statement_in_this_lane_names_the_label_table() -> None:
    # Asserted through the census's OWN discovery, not a text grep, so it means
    # exactly what the census means: of every SQL statement this lane executes,
    # none names image_tag_labels. That is why the census stays green with no
    # _EXEMPT entry — there is nothing here for it to find.
    from tests.sql_corpus import discover

    mine = ("toolkit/tag_heads.py", "toolkit/tag_heads_eval.py",
            "scripts/train_tag_head.py")
    items = [i for i in discover(include_inline=True, resolve_imports=True)
             if any(i.origin.startswith(m) for m in mine)]
    assert items, "discovery found no SQL in this lane at all — the scan is broken"
    offenders = [i.origin for i in items if "image_tag_labels" in i.sql]
    assert not offenders, offenders


def test_the_sql_this_lane_does_own_touches_only_two_tables() -> None:
    for sql in (th._DOMINANT_ENCODER_SQL, th._EMBEDDINGS_SQL, th._IMAGE_GROUPS_SQL):
        assert "image_tag_labels" not in sql
        assert "tag_exam" not in sql
    assert "image_dinov3_embeddings" in th._EMBEDDINGS_SQL
    assert "FROM images" in th._IMAGE_GROUPS_SQL


# ------------------------------------------------------------------ assembly

def test_the_encoder_default_is_whichever_has_the_most_vectors() -> None:
    conn = _conn()
    snap = th.assemble_dataset(conn, tag_id=19)
    assert snap.encoder == ENCODER
    # ...and every embedding read is pinned to all seven identity facts.
    emb_calls = [p for s, p in conn.executed if "FROM image_dinov3_embeddings e" in s]
    assert emb_calls
    for params in emb_calls:
        for f in th.ENCODER_FIELDS:
            assert params[f] == ENCODER.as_dict()[f]


def test_a_named_encoder_that_stored_nothing_yields_an_empty_dataset() -> None:
    other = th.EncoderIdentity.from_dict({**ENCODER.as_dict(), "model": "other/model"})
    conn = _conn()
    snap = th.assemble_dataset(conn, tag_id=19, encoder=other)
    assert snap.rows == ()
    # Silence would be the bug: every labeled image is REPORTED as vectorless.
    assert len(snap.missing_embedding) == len(conn.positive_ids) + len(conn.negative_ids)


def test_vectors_parse_from_both_the_text_and_sequence_shapes() -> None:
    as_text = th.assemble_dataset(_conn(as_text=True), tag_id=19)
    as_seq = th.assemble_dataset(_conn(as_text=False), tag_id=19)
    assert as_text.dataset_hash == as_seq.dataset_hash
    assert [r.embedding for r in as_text.rows] == [r.embedding for r in as_seq.rows]


def test_an_image_labeled_both_ways_is_dropped_and_named() -> None:
    conn = _conn()
    conflicted = conn.positive_ids[0]
    conn.negative_ids.append(conflicted)
    snap = th.assemble_dataset(conn, tag_id=19)
    assert snap.conflicting == (conflicted,)
    assert conflicted not in {r.image_id for r in snap.rows}


def test_an_image_with_no_listing_gets_a_group_of_its_own() -> None:
    conn = _conn()
    orphan = conn.positive_ids[0]
    conn.groups[orphan] = None
    snap = th.assemble_dataset(conn, tag_id=19)
    assert snap.missing_group == (orphan,)
    row = next(r for r in snap.rows if r.image_id == orphan)
    assert row.listing_id == -orphan
    assert sum(1 for r in snap.rows if r.listing_id == row.listing_id) == 1


def test_a_tag_with_no_labels_is_an_error_not_an_empty_head() -> None:
    conn = _FakeConn(positive_ids=[], negative_ids=[], embeddings={}, groups={})
    with pytest.raises(th.TagHeadError, match="no trainable labels"):
        th.assemble_dataset(conn, tag_id=19)


# ---------------------------------------------------------------- the hash

def test_the_hash_is_deterministic_regardless_of_input_order() -> None:
    rows = [(3, 30, 1), (1, 10, 0), (2, 20, 1)]
    a = th.dataset_hash(tag_id=19, encoder=ENCODER, rows=rows)
    b = th.dataset_hash(tag_id=19, encoder=ENCODER, rows=list(reversed(rows)))
    assert a == b


def test_the_hash_moves_when_one_label_moves() -> None:
    rows = [(1, 10, 1), (2, 20, 0)]
    flipped = [(1, 10, 1), (2, 20, 1)]
    assert th.dataset_hash(tag_id=19, encoder=ENCODER, rows=rows) != \
        th.dataset_hash(tag_id=19, encoder=ENCODER, rows=flipped)


def test_the_hash_moves_when_the_encoder_or_the_tag_moves() -> None:
    rows = [(1, 10, 1), (2, 20, 0)]
    base = th.dataset_hash(tag_id=19, encoder=ENCODER, rows=rows)
    other = th.EncoderIdentity.from_dict({**ENCODER.as_dict(), "resolution": 448})
    assert th.dataset_hash(tag_id=19, encoder=other, rows=rows) != base
    assert th.dataset_hash(tag_id=2, encoder=ENCODER, rows=rows) != base


def test_the_hash_does_not_move_with_float_noise_in_the_vectors() -> None:
    # Deliberate: the hash names the POPULATION, not the bytes of 768 floats.
    a = th.assemble_dataset(_conn(seed=7), tag_id=19)
    conn = _conn(seed=7)
    for k in conn.embeddings:
        conn.embeddings[k] = [x + 1e-9 for x in conn.embeddings[k]]
    b = th.assemble_dataset(conn, tag_id=19)
    assert a.dataset_hash == b.dataset_hash


def test_assembly_reaches_the_same_hash_whatever_order_the_doors_answer_in() -> None:
    a = th.assemble_dataset(_conn(), tag_id=19)
    conn = _conn()
    random.Random(1).shuffle(conn.positive_ids)
    random.Random(2).shuffle(conn.negative_ids)
    assert th.assemble_dataset(conn, tag_id=19).dataset_hash == a.dataset_hash


# ------------------------------------------------------------- the grouping

def test_no_listing_straddles_a_fold() -> None:
    pytest.importorskip("sklearn")
    # per_listing=6 is the leakage-prone shape: a naive random split would put
    # five of a listing's photos in train and one in eval nearly every time.
    snap = th.assemble_dataset(_conn(n_listings=20, per_listing=6), tag_id=19)
    folds = th.grouped_folds(snap, n_splits=4)
    assert len(folds) == 4
    for train_idx, eval_idx in folds:
        train_groups = {snap.rows[i].listing_id for i in train_idx}
        eval_groups = {snap.rows[i].listing_id for i in eval_idx}
        assert not (train_groups & eval_groups)


def test_every_row_is_graded_exactly_once_across_the_folds() -> None:
    pytest.importorskip("sklearn")
    snap = th.assemble_dataset(_conn(n_listings=20, per_listing=6), tag_id=19)
    graded: list[int] = []
    for _, eval_idx in th.grouped_folds(snap, n_splits=4):
        graded.extend(eval_idx)
    assert sorted(graded) == list(range(len(snap.rows)))


def test_a_set_too_thin_to_grade_is_refused_rather_than_scored() -> None:
    pytest.importorskip("sklearn")
    conn = _FakeConn(
        positive_ids=[1, 2, 3], negative_ids=[4],
        embeddings={i: [1.0, 0.0] if i < 4 else [-1.0, 0.0] for i in (1, 2, 3, 4)},
        groups={1: 10, 2: 10, 3: 10, 4: 11})
    snap = th.assemble_dataset(conn, tag_id=19)
    with pytest.raises(th.TagHeadError, match="listing-groups"):
        th.train_head(snap, trained_at=TRAINED_AT)


# ------------------------------------------------- end to end, synthetically

def _train(**kw):
    pytest.importorskip("sklearn")
    snap = th.assemble_dataset(_conn(**kw), tag_id=19)
    return snap, th.train_head(snap, trained_at=TRAINED_AT, n_splits=4)


def test_the_whole_pipeline_learns_a_separable_synthetic_tag() -> None:
    snap, head = _train(n_listings=24, per_listing=3)
    m = head.metrics
    assert m.graded_n == len(snap.rows)
    assert m.graded_positive_n + m.graded_negative_n == m.graded_n
    assert m.precision >= 0.9 and m.recall >= 0.9, m
    assert m.strategy == "StratifiedGroupKFold" and m.n_splits == 4
    assert m.true_positives + m.false_negatives == m.graded_positive_n


def test_it_does_not_hardcode_a_smaller_dimension_than_the_real_encoder() -> None:
    snap, head = _train(dim=768, n_listings=12, per_listing=2)
    assert snap.dimension == 768
    assert len(head.artifact["weights"]) == 768
    assert head.artifact["dimension"] == 768
    assert head.metrics.precision >= 0.9 and head.metrics.recall >= 0.9


def test_the_artifact_carries_what_a_later_reader_needs(tmp_path) -> None:
    snap, head = _train()
    a = head.artifact
    assert a["tag_id"] == 19
    assert a["encoder"] == ENCODER.as_dict()
    assert a["dataset_hash"] == snap.dataset_hash
    assert a["trained_at"] == TRAINED_AT.isoformat()
    assert a["n_positive"] == snap.n_positive and a["n_negative"] == snap.n_negative
    assert a["metrics"]["graded_n"] == head.metrics.graded_n
    assert a["sklearn_version"]
    assert th.artifact_encoder(a) == ENCODER


def test_the_artifact_is_reproducible_for_the_same_inputs() -> None:
    pytest.importorskip("sklearn")
    snap = th.assemble_dataset(_conn(), tag_id=19)
    one = th.train_head(snap, trained_at=TRAINED_AT, n_splits=4).artifact
    two = th.train_head(snap, trained_at=TRAINED_AT, n_splits=4).artifact
    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)


def test_a_reloaded_artifact_predicts_what_the_fitted_model_predicts(tmp_path) -> None:
    snap, head = _train()
    path = th.save_artifact(head.artifact, tmp_path / "heads" / "tag-19.json")
    reloaded = th.load_artifact(path)

    vectors = [list(r.embedding) for r in snap.rows]
    from_sklearn = head.estimator.predict_proba(vectors)
    for row, probs in zip(snap.rows, from_sklearn):
        mine = th.score_embedding(reloaded, row.embedding)
        assert math.isclose(mine, float(probs[1]), rel_tol=1e-9, abs_tol=1e-9)
        assert th.predict(reloaded, row.embedding) == (float(probs[1]) >= 0.5)


def test_scoring_an_artifact_needs_no_sklearn_at_all() -> None:
    # Hand-built artifact: the inference path is a dot product and a logistic, so
    # this runs in the default CI lane where the training extra is absent.
    artifact = {
        "artifact_version": th.ARTIFACT_VERSION, "kind": th.ARTIFACT_KIND,
        "tag_id": 19, "encoder": ENCODER.as_dict(), "dataset_hash": "x",
        "trained_at": TRAINED_AT.isoformat(), "threshold": 0.5,
        "metrics": {}, "weights": [2.0, -1.0], "bias": 0.5,
    }
    assert math.isclose(th.score_embedding(artifact, [1.0, 0.0]),
                        1 / (1 + math.exp(-2.5)))
    assert th.predict(artifact, [1.0, 0.0]) is True
    assert th.predict(artifact, [-1.0, 0.0]) is False


def test_a_wrong_width_vector_is_refused_not_silently_truncated() -> None:
    artifact = {"weights": [1.0, 2.0], "bias": 0.0, "threshold": 0.5}
    with pytest.raises(th.TagHeadError, match="3 dims"):
        th.score_embedding(artifact, [1.0, 2.0, 3.0])


def test_load_artifact_refuses_the_wrong_thing(tmp_path) -> None:
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"kind": "something_else"}), encoding="utf-8")
    with pytest.raises(th.TagHeadError, match="missing"):
        th.load_artifact(p)

    good = {
        "artifact_version": 99, "kind": th.ARTIFACT_KIND, "tag_id": 1,
        "encoder": ENCODER.as_dict(), "dataset_hash": "x", "trained_at": "t",
        "threshold": 0.5, "metrics": {}, "weights": [1.0], "bias": 0.0,
    }
    p.write_text(json.dumps(good), encoding="utf-8")
    with pytest.raises(th.TagHeadError, match="version 99"):
        th.load_artifact(p)

    p.write_text(json.dumps({**good, "artifact_version": th.ARTIFACT_VERSION,
                             "kind": "clip_probe"}), encoding="utf-8")
    with pytest.raises(th.TagHeadError, match="not a tag head"):
        th.load_artifact(p)


def test_the_graded_n_is_a_first_class_field_not_a_log_line() -> None:
    # The whole point of reporting n beside a proportion.
    names = set(th.HeadMetrics.__dataclass_fields__)
    assert {"graded_n", "graded_positive_n", "graded_negative_n"} <= names
    assert {"true_positives", "false_positives",
            "true_negatives", "false_negatives"} <= names


def test_there_is_no_list_of_target_tags_anywhere_in_this_lane() -> None:
    # Gate 1's list is the operator's and is not final. A head is per-tag; the tag
    # is an argument. A hard-coded tuple of ids here would be a second source of
    # truth for a decision that has not been made.
    import inspect
    src = inspect.getsource(th)
    assert "tag_id" in src
    for name, value in vars(th).items():
        if name.isupper() and isinstance(value, (list, tuple, set)):
            assert not all(isinstance(v, int) for v in value) or not value, \
                f"{name} looks like a hard-coded tag list"
