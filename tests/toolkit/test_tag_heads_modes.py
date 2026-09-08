"""The two positive-only training modes, and the injectable vector source.

SYNTHETIC AND OFFLINE, like tests/toolkit/test_tag_heads.py — no database, no
real label, no real photo. The properties under test are structural, and they are
the ones an encoder bake-off's conclusion rests on:

  * the modes differ in what the FIT sees and never in what is GRADED, so
    "positive-only won" cannot mean "positive-only was asked something easier";
  * a free negative never straddles a fold, so the grouped split's guarantee
    survives the extra rows;
  * an injected vector source changes which vectors a head sees and nothing about
    which labels it trains on — the one door stays the one door.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

import pytest

from toolkit import tag_heads as th

from tests.toolkit.test_tag_heads import ENCODER, _FakeConn, _make_corpus

TRAINED_AT = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


def _snapshot(**kw: Any) -> th.DatasetSnapshot:
    pos, neg, emb, grp = _make_corpus(**kw)
    conn = _FakeConn(positive_ids=pos, negative_ids=neg, embeddings=emb, groups=grp)
    return th.assemble_dataset(conn, tag_id=19, encoder=ENCODER)


def _free_negatives(snapshot: th.DatasetSnapshot, *, count: int = 40,
                    seed: int = 11) -> list[th.DatasetRow]:
    """Rows from listings that appear NOWHERE in the head's own population, so
    the only leakage this could introduce is one the code must prevent itself."""
    rnd = random.Random(seed)
    dim = snapshot.dimension
    used = {r.listing_id for r in snapshot.rows}
    rows = []
    image_id = 900_000
    for k in range(count):
        image_id += 1
        listing = 500_000 + k
        assert listing not in used
        # Orthogonal-ish to both classes: a genuine "something else entirely".
        vec = [rnd.gauss(0.0, 0.2) for _ in range(dim)]
        vec[1] = 1.0
        rows.append(th.DatasetRow(image_id=image_id, listing_id=listing,
                                  label=0, embedding=tuple(vec)))
    return rows


# ------------------------------------------------ the injectable vector source

def test_mapping_vector_source_feeds_the_dataset_and_no_embedding_sql_runs() -> None:
    pos, neg, emb, grp = _make_corpus()
    conn = _FakeConn(positive_ids=pos, negative_ids=neg, embeddings={}, groups=grp)
    snap = th.assemble_dataset(conn, tag_id=19, encoder=ENCODER,
                               vectors=th.mapping_vector_source(emb))

    assert len(snap.rows) == len(pos) + len(neg)
    assert not [s for s, _ in conn.executed if "image_dinov3_embeddings" in s], \
        "an injected source must replace the production read, not supplement it"
    # And the label door was still the only label read.
    assert conn.doors_used == ["training_rows"]


def test_a_plain_mapping_is_accepted_as_a_source() -> None:
    pos, neg, emb, grp = _make_corpus()
    conn = _FakeConn(positive_ids=pos, negative_ids=neg, embeddings={}, groups=grp)
    snap = th.assemble_dataset(conn, tag_id=19, encoder=ENCODER, vectors=emb)
    assert len(snap.rows) == len(pos) + len(neg)


def test_an_injected_source_must_name_its_encoder() -> None:
    pos, neg, emb, grp = _make_corpus()
    conn = _FakeConn(positive_ids=pos, negative_ids=neg, embeddings={}, groups=grp)
    with pytest.raises(th.TagHeadError, match="encoder identity"):
        th.assemble_dataset(conn, tag_id=19, vectors=emb)


def test_an_injected_source_reports_gaps_rather_than_shrinking_silently() -> None:
    pos, neg, emb, grp = _make_corpus()
    partial = {k: v for k, v in emb.items() if k not in pos[:3]}
    conn = _FakeConn(positive_ids=pos, negative_ids=neg, embeddings={}, groups=grp)
    snap = th.assemble_dataset(conn, tag_id=19, encoder=ENCODER, vectors=partial)
    assert set(snap.missing_embedding) == set(pos[:3])


# --------------------------------------------------------- what each mode fits

def test_pos_neg_fits_on_every_training_row() -> None:
    snap = _snapshot()
    folds = th.grouped_folds(snap, n_splits=3, seed=0)
    train_idx, eval_idx = folds[0]
    fitting = th._fold_training_rows(snap, train_idx, eval_idx,
                                     mode=th.MODE_POS_NEG, free_negatives=())
    assert len(fitting) == len(train_idx)
    assert {r.label for r in fitting} == {0, 1}


def test_pos_only_centroid_fits_on_positives_alone() -> None:
    snap = _snapshot()
    train_idx, eval_idx = th.grouped_folds(snap, n_splits=3, seed=0)[0]
    fitting = th._fold_training_rows(snap, train_idx, eval_idx,
                                     mode=th.MODE_POS_ONLY_CENTROID,
                                     free_negatives=())
    assert fitting, "a centroid still needs its positives"
    assert {r.label for r in fitting} == {1}, \
        "the strict positive-only reading fits on NO negative at all"


def test_pos_only_free_neg_uses_no_operator_negative() -> None:
    snap = _snapshot()
    free = _free_negatives(snap)
    train_idx, eval_idx = th.grouped_folds(snap, n_splits=3, seed=0)[0]
    fitting = th._fold_training_rows(snap, train_idx, eval_idx,
                                     mode=th.MODE_POS_ONLY_FREE_NEG,
                                     free_negatives=free)
    own_negatives = {r.image_id for r in snap.rows if r.label == 0}
    assert not ({r.image_id for r in fitting} & own_negatives), \
        "the free-negative mode must not reach the labels it exists to do without"
    assert {r.label for r in fitting} == {0, 1}


def test_free_negatives_never_straddle_the_evaluated_fold() -> None:
    # Give a free negative a listing_id that IS in the head's own population, and
    # it must be dropped from any fold that grades that listing — otherwise the
    # grouped split's whole guarantee is gone.
    snap = _snapshot()
    victim = snap.rows[0]
    intruder = th.DatasetRow(image_id=777_777, listing_id=victim.listing_id,
                             label=0, embedding=victim.embedding)
    for train_idx, eval_idx in th.grouped_folds(snap, n_splits=3, seed=0):
        fitting = th._fold_training_rows(snap, train_idx, eval_idx,
                                         mode=th.MODE_POS_ONLY_FREE_NEG,
                                         free_negatives=[intruder])
        graded_groups = {snap.rows[i].listing_id for i in eval_idx}
        assert not [r for r in fitting if r.listing_id in graded_groups
                    and r.image_id == intruder.image_id]


# ------------------------------------------------- every mode grades the same rows

@pytest.mark.parametrize("mode", th.MODES)
def test_every_mode_grades_exactly_the_heads_own_population(mode: str) -> None:
    pytest.importorskip("sklearn")
    snap = _snapshot()
    head = th.train_head(snap, trained_at=TRAINED_AT, mode=mode,
                         free_negatives=_free_negatives(snap), n_splits=3)
    assert {p.image_id for p in head.oof} == {r.image_id for r in snap.rows}, \
        "a mode that grades a different population is not comparable to the others"
    assert head.metrics.graded_n == len(snap.rows)
    assert head.mode == mode


@pytest.mark.parametrize("mode", th.MODES)
def test_every_mode_separates_a_linearly_separable_corpus(mode: str) -> None:
    pytest.importorskip("sklearn")
    snap = _snapshot()
    head = th.train_head(snap, trained_at=TRAINED_AT, mode=mode,
                         free_negatives=_free_negatives(snap), n_splits=3)
    assert head.metrics.f1 > 0.8, (mode, head.metrics.as_dict())


def test_centroid_mode_reports_the_threshold_it_chose() -> None:
    pytest.importorskip("sklearn")
    snap = _snapshot()
    head = th.train_head(snap, trained_at=TRAINED_AT,
                         mode=th.MODE_POS_ONLY_CENTROID, n_splits=3)
    # A cosine, not a probability — so it must NOT be the logistic default, and
    # the artifact must carry whatever was chosen so the number is reproducible.
    assert head.threshold == head.artifact["threshold"]
    assert -1.0 <= head.threshold <= 1.0
    assert head.artifact["hyperparameters"]["threshold_rule"] == "max_f1_oof"


def test_centroid_artifact_is_its_own_kind_and_scores_without_sklearn() -> None:
    pytest.importorskip("sklearn")
    snap = _snapshot()
    head = th.train_head(snap, trained_at=TRAINED_AT,
                         mode=th.MODE_POS_ONLY_CENTROID, n_splits=3)
    assert head.artifact["kind"] == th.ARTIFACT_KIND_CENTROID
    assert "weights" not in head.artifact and "bias" not in head.artifact
    assert head.estimator is None
    positive = next(r for r in snap.rows if r.label == 1)
    negative = next(r for r in snap.rows if r.label == 0)
    assert th.score_embedding(head.artifact, positive.embedding) > \
        th.score_embedding(head.artifact, negative.embedding)


def test_centroid_artifact_round_trips_through_the_loader(tmp_path) -> None:
    pytest.importorskip("sklearn")
    snap = _snapshot()
    head = th.train_head(snap, trained_at=TRAINED_AT,
                         mode=th.MODE_POS_ONLY_CENTROID, n_splits=3)
    path = th.save_artifact(head.artifact, tmp_path / "centroid.json")
    loaded = th.load_artifact(path)
    assert loaded == head.artifact


def test_a_logreg_artifact_missing_its_weights_is_rejected(tmp_path) -> None:
    pytest.importorskip("sklearn")
    snap = _snapshot()
    head = th.train_head(snap, trained_at=TRAINED_AT, n_splits=3)
    broken = {k: v for k, v in head.artifact.items() if k != "weights"}
    path = th.save_artifact(broken, tmp_path / "broken.json")
    with pytest.raises(th.TagHeadError, match="weights"):
        th.load_artifact(path)


def test_free_neg_artifact_records_how_many_free_negatives_it_used() -> None:
    pytest.importorskip("sklearn")
    snap = _snapshot()
    free = _free_negatives(snap, count=17)
    head = th.train_head(snap, trained_at=TRAINED_AT,
                         mode=th.MODE_POS_ONLY_FREE_NEG, free_negatives=free,
                         n_splits=3)
    assert head.artifact["n_free_negative"] == 17
    assert head.artifact["mode"] == th.MODE_POS_ONLY_FREE_NEG


def test_out_of_fold_rows_carry_the_fold_that_graded_them() -> None:
    pytest.importorskip("sklearn")
    snap = _snapshot()
    head = th.train_head(snap, trained_at=TRAINED_AT, n_splits=3)
    assert {p.fold for p in head.oof} == {0, 1, 2}


def test_an_unknown_mode_is_refused() -> None:
    snap = _snapshot()
    with pytest.raises(th.TagHeadError, match="unknown training mode"):
        th.train_head(snap, trained_at=TRAINED_AT, mode="pos_only_vibes")
