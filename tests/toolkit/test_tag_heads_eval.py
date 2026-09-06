"""The nearest-neighbor diagnostic, and the wall between it and the metrics.

Synthetic vectors only — no database, no real label. The diagnostic exists to help
a human read a mistake; the tests below pin both halves of that: it finds the
neighbor it claims to find, and it cannot reach the numbers.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from toolkit import tag_heads as th
from toolkit import tag_heads_eval as te

ENCODER = th.EncoderIdentity(
    model="facebook/dinov3-vitb16", revision="deadbeef", library="transformers",
    pooling="cls", resolution=224, preprocessing="p", dtype="float32",
)


def _row(image_id: int, listing_id: int, label: int, vec) -> th.DatasetRow:
    return th.DatasetRow(image_id=image_id, listing_id=listing_id, label=label,
                         embedding=tuple(float(x) for x in vec))


def _snapshot(rows) -> th.DatasetSnapshot:
    return th.DatasetSnapshot(
        tag_id=19, encoder=ENCODER, rows=tuple(rows),
        dataset_hash=th.dataset_hash(
            tag_id=19, encoder=ENCODER,
            rows=[(r.image_id, r.listing_id, r.label) for r in rows]))


def _oof(image_id, listing_id, label, score, predicted) -> th.OofPrediction:
    return th.OofPrediction(image_id=image_id, listing_id=listing_id, label=label,
                            score=score, predicted=predicted)


# ------------------------------------------------------------ the diagnostic

def test_it_finds_the_known_nearest_positive_for_a_false_positive() -> None:
    pytest.importorskip("faiss")
    # image 1 is a NEGATIVE the head called positive. Positive 2 points almost
    # exactly the same way; positive 3 is orthogonal. The twin is the answer.
    snap = _snapshot([
        _row(1, 10, 0, [1.0, 0.02, 0.0]),
        _row(2, 20, 1, [1.0, 0.00, 0.0]),
        _row(3, 30, 1, [0.0, 0.00, 1.0]),
        _row(4, 40, 0, [-1.0, 0.0, 0.0]),
    ])
    out = te.nearest_opposite_neighbors(snap, [_oof(1, 10, 0, 0.9, 1)])
    assert len(out) == 1
    d = out[0]
    assert d.kind == "false_positive"
    assert d.neighbor_image_id == 2 and d.neighbor_label == 1
    assert d.similarity > 0.99


def test_a_false_negative_is_matched_against_the_genuine_negatives() -> None:
    pytest.importorskip("faiss")
    snap = _snapshot([
        _row(1, 10, 1, [-1.0, 0.03, 0.0]),
        _row(2, 20, 0, [-1.0, 0.00, 0.0]),
        _row(3, 30, 0, [0.0, 1.00, 0.0]),
        _row(4, 40, 1, [1.0, 0.00, 0.0]),
    ])
    out = te.nearest_opposite_neighbors(snap, [_oof(1, 10, 1, 0.1, 0)])
    assert [(d.kind, d.neighbor_image_id, d.neighbor_label) for d in out] == \
        [("false_negative", 2, 0)]


def test_similarity_is_cosine_so_length_does_not_decide_the_neighbor() -> None:
    pytest.importorskip("faiss")
    # A hugely-scaled but differently-pointing positive must lose to a short one
    # pointing the same way: inner product on NORMALIZED vectors, as documented.
    snap = _snapshot([
        _row(1, 10, 0, [1.0, 0.0, 0.0]),
        _row(2, 20, 1, [0.001, 0.0, 0.0]),
        _row(3, 30, 1, [50.0, 50.0, 0.0]),
    ])
    out = te.nearest_opposite_neighbors(snap, [_oof(1, 10, 0, 0.9, 1)])
    assert out[0].neighbor_image_id == 2


def test_mistakes_are_only_the_wrong_ones_most_confident_first() -> None:
    oof = [
        _oof(1, 10, 1, 0.55, 1),   # right
        _oof(2, 20, 0, 0.60, 1),   # wrong, mildly
        _oof(3, 30, 0, 0.99, 1),   # wrong, confidently
        _oof(4, 40, 1, 0.02, 0),   # wrong, confidently
    ]
    assert [m.image_id for m in te.mistakes(oof)] == [3, 4, 2]


def test_no_mistakes_means_no_diagnostics_and_no_faiss_import() -> None:
    snap = _snapshot([_row(1, 10, 1, [1.0, 0.0])])
    assert te.nearest_opposite_neighbors(snap, []) == ()


def test_a_mistake_is_never_its_own_neighbor() -> None:
    pytest.importorskip("faiss")
    # Passing a non-mistake (label == predicted) puts the query INSIDE the pool;
    # the guard must skip the self-match rather than report cos=1.0 with itself.
    snap = _snapshot([
        _row(1, 10, 1, [1.0, 0.0]),
        _row(2, 20, 1, [0.99, 0.01]),
    ])
    out = te.nearest_opposite_neighbors(snap, [_oof(1, 10, 1, 0.9, 1)])
    assert [d.neighbor_image_id for d in out] == [2]


# -------------------------------------------------- the wall around the metrics

def test_diagnose_head_passes_the_metrics_through_untouched() -> None:
    pytest.importorskip("faiss")
    snap = _snapshot([
        _row(1, 10, 0, [1.0, 0.02]),
        _row(2, 20, 1, [1.0, 0.00]),
        _row(3, 30, 1, [0.0, 1.00]),
        _row(4, 40, 0, [-1.0, 0.0]),
    ])
    oof = (_oof(1, 10, 0, 0.9, 1), _oof(2, 20, 1, 0.9, 1),
           _oof(3, 30, 1, 0.8, 1), _oof(4, 40, 0, 0.1, 0))
    metrics = th._metrics(oof, n_splits=2, strategy="StratifiedGroupKFold")
    head = th.TrainedHead(artifact={}, metrics=metrics, snapshot=snap, oof=oof)

    report = te.diagnose_head(head)
    assert report.metrics is metrics          # the SAME object, not a recomputation
    assert report.metrics == metrics
    assert report.diagnostics                  # and the analysis is beside it
    assert report.tag_id == 19
    assert report.dataset_hash == snap.dataset_hash


def test_a_diagnostic_cannot_be_smuggled_into_the_metrics() -> None:
    fields = set(th.HeadMetrics.__dataclass_fields__)
    for banned in ("diagnostic", "neighbor", "nearest", "similarity", "faiss"):
        assert not any(banned in f for f in fields), fields
    # Frozen, so it cannot be mutated after the fact either.
    assert th.HeadMetrics.__dataclass_params__.frozen
    m = th._metrics((), n_splits=2, strategy="s")
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.diagnostics = ()          # type: ignore[attr-defined]


def test_the_metrics_dict_written_into_an_artifact_carries_no_diagnostics() -> None:
    m = th._metrics((_oof(1, 10, 1, 0.9, 1),), n_splits=2, strategy="s")
    assert "diagnostics" not in m.as_dict()
    assert set(m.as_dict()) == set(th.HeadMetrics.__dataclass_fields__)


def test_the_trainer_never_reads_the_diagnostics() -> None:
    # Structural: the training module does not import the eval module at all, so
    # no diagnostic can reach a weight, a metric or a threshold.
    src = inspect.getsource(th)
    assert "tag_heads_eval" not in src
    assert "faiss" not in src
    # ...and the eval module only ever consumes an already-finished head.
    eval_src = inspect.getsource(te)
    assert "train_head" not in eval_src
    assert "LogisticRegression" not in eval_src
