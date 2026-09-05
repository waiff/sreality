"""One binary logistic head per tag, over the frozen DINOv3 embeddings.

Shape: ONE independent yes/no head per target tag — not one multinomial head over
many classes. `docs/design/clip-linear-probe.md` is still the methodology (grouped
splits D4.2, content-hashed dataset snapshot D4.1, canonical lbfgs/class-balanced
protocol D5, an artifact whose inference path needs no ML library), but its single
multi-class head is superseded: `image_tag_labels` carries a per-(image, tag)
tri-state, so a per-tag head trains on real negatives instead of "not the labeled
class". The target tag is an ARGUMENT — there is no list of "the 12 tags" in this
module, because Gate 1's list is the operator's and is not finalized.

THE TWO DOORS. Training labels are read through `toolkit.machine_labeling` and
`toolkit.tag_holdout` and nowhere else. This module contains no SQL naming
image_tag_labels, deliberately:

  * positives = machine_labeling.training_set_positive_ids — the SET as the
    operator's own review page defines it (their confirmed positives first, then
    the machine's oldest-first, up to tag_taxonomy.training_target). What a human
    reviewed and what the head trains on therefore cannot diverge.
  * negatives = tag_holdout.training_label_rows(states=("negative",)) — human-only,
    sealed exam excluded. `include_holdout` is never passed: that door is the
    operator's, and opening it would cost this head the ability to be graded on
    the holdout it consumed.

There is no sanctioned door for machine-labeled negatives, so there are none here.
`tests/test_holdout_exclusion_census.py` is the rail; this module stays off its
radar by never writing the statement in the first place.

scikit-learn is a training-only extra (`pip install -e ".[training]"`) and is
imported lazily inside the training functions. Assembling a dataset, loading an
artifact and scoring an embedding from it are pure stdlib.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import psycopg

from toolkit import machine_labeling, tag_holdout

ARTIFACT_VERSION = 1
ARTIFACT_KIND = "tag_head_binary_logreg"

DEFAULT_N_SPLITS = 5
DEFAULT_C = 1.0
DEFAULT_THRESHOLD = 0.5
DEFAULT_MAX_ITER = 1000
DEFAULT_SEED = 0

# The composite primary key of image_dinov3_embeddings minus image_id: what
# "which encoder produced this vector" means. Deliberately a local, minimal
# representation — no shared EncoderConfig type is imported from the job that
# writes the rows, so the two can be built and reviewed independently.
ENCODER_FIELDS = (
    "model", "revision", "library", "pooling", "resolution", "preprocessing", "dtype",
)

# Chunked so a tag whose set grows well past the ~300 target still sends one
# bounded array parameter per statement.
_ID_CHUNK = 2000


class TagHeadError(RuntimeError):
    """A dataset or a fit that cannot honestly produce a graded head."""


# --- encoder identity -------------------------------------------------------

@dataclass(frozen=True)
class EncoderIdentity:
    model: str
    revision: str
    library: str
    pooling: str
    resolution: int
    preprocessing: str
    dtype: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model, "revision": self.revision, "library": self.library,
            "pooling": self.pooling, "resolution": int(self.resolution),
            "preprocessing": self.preprocessing, "dtype": self.dtype,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EncoderIdentity":
        missing = [f for f in ENCODER_FIELDS if f not in raw]
        if missing:
            raise TagHeadError(f"encoder identity is missing {', '.join(missing)}")
        return cls(
            model=str(raw["model"]), revision=str(raw["revision"]),
            library=str(raw["library"]), pooling=str(raw["pooling"]),
            resolution=int(raw["resolution"]), preprocessing=str(raw["preprocessing"]),
            dtype=str(raw["dtype"]),
        )


# Convenience default only: "whichever encoder has the most vectors stored right
# now". A head trained against a guess is still pinned by the artifact — the seven
# facts are written into it — so the risk of the convenience is a wasted run, not a
# silently mixed one.
_DOMINANT_ENCODER_SQL = """
    SELECT model, revision, library, pooling, resolution, preprocessing, dtype,
           count(*)::bigint AS n
    FROM image_dinov3_embeddings
    GROUP BY model, revision, library, pooling, resolution, preprocessing, dtype
    ORDER BY n DESC, model, revision, library, pooling, resolution, preprocessing, dtype
    LIMIT 1
"""

_EMBEDDINGS_SQL = """
    SELECT e.image_id, e.embedding
    FROM image_dinov3_embeddings e
    WHERE e.image_id = ANY(%(image_ids)s::bigint[])
      AND e.model = %(model)s
      AND e.revision = %(revision)s
      AND e.library = %(library)s
      AND e.pooling = %(pooling)s
      AND e.resolution = %(resolution)s::int
      AND e.preprocessing = %(preprocessing)s
      AND e.dtype = %(dtype)s
    ORDER BY e.image_id
"""

# The grouped split's group key. One statement, one table: nothing here can reach
# a label.
_IMAGE_GROUPS_SQL = """
    SELECT i.id, i.listing_id
    FROM images i
    WHERE i.id = ANY(%(image_ids)s::bigint[])
    ORDER BY i.id
"""


def dominant_encoder(conn: psycopg.Connection) -> EncoderIdentity | None:
    """The encoder identity with the most stored vectors, or None if the table is
    empty. A convenience default for `assemble_dataset`, never a substitute for
    the caller naming the encoder it means."""
    with conn.cursor() as cur:
        cur.execute(_DOMINANT_ENCODER_SQL)
        row = cur.fetchone()
    if not row:
        return None
    return EncoderIdentity(
        model=str(row[0]), revision=str(row[1]), library=str(row[2]),
        pooling=str(row[3]), resolution=int(row[4]), preprocessing=str(row[5]),
        dtype=str(row[6]),
    )


# --- the dataset ------------------------------------------------------------

@dataclass(frozen=True)
class DatasetRow:
    image_id: int
    listing_id: int
    label: int                      # 1 positive, 0 negative
    embedding: tuple[float, ...]


@dataclass(frozen=True)
class DatasetSnapshot:
    """One tag's trainable population, frozen: which images, which labels, which
    group each belongs to, under which encoder — plus the hash that names it.

    The hash covers the labels, the images, their group assignment and the encoder
    identity; NOT the vectors. Hashing 768 floats per row would make the identity
    of a dataset hostage to float formatting, while the thing worth detecting — a
    label changed, an image added, a different encoder read — is fully covered here.
    """
    tag_id: int
    encoder: EncoderIdentity
    rows: tuple[DatasetRow, ...]
    dataset_hash: str
    missing_embedding: tuple[int, ...] = ()
    missing_group: tuple[int, ...] = ()
    conflicting: tuple[int, ...] = ()

    @property
    def n_positive(self) -> int:
        return sum(1 for r in self.rows if r.label == 1)

    @property
    def n_negative(self) -> int:
        return sum(1 for r in self.rows if r.label == 0)

    @property
    def n_groups(self) -> int:
        return len({r.listing_id for r in self.rows})

    @property
    def dimension(self) -> int:
        return len(self.rows[0].embedding) if self.rows else 0


def dataset_hash(
    *, tag_id: int, encoder: EncoderIdentity,
    rows: Iterable[tuple[int, int, int]],
) -> str:
    """sha256 over a deterministic serialization of (tag, encoder, labelled rows).

    Sorted by image_id so the hash is a property of the population, not of the
    order two doors happened to return it in."""
    payload = {
        "v": ARTIFACT_VERSION,
        "tag_id": int(tag_id),
        "encoder": encoder.as_dict(),
        "rows": sorted([int(i), int(g), int(l)] for i, g, l in rows),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunks(ids: Sequence[int], size: int = _ID_CHUNK):
    for i in range(0, len(ids), size):
        yield list(ids[i:i + size])


def _parse_vector(raw: Any) -> tuple[float, ...]:
    """pgvector/halfvec arrives as '[0.1,0.2,...]' unless the adapter is
    registered; accept both that and an already-sequence value."""
    if isinstance(raw, str):
        return tuple(float(x) for x in raw.strip("[]").split(",") if x.strip())
    return tuple(float(x) for x in raw)


def _fetch_embeddings(
    conn: psycopg.Connection, image_ids: Sequence[int], encoder: EncoderIdentity,
) -> dict[int, tuple[float, ...]]:
    out: dict[int, tuple[float, ...]] = {}
    params = encoder.as_dict()
    for chunk in _chunks(image_ids):
        with conn.cursor() as cur:
            cur.execute(_EMBEDDINGS_SQL, {**params, "image_ids": chunk})
            for image_id, vec in cur.fetchall():
                out[int(image_id)] = _parse_vector(vec)
    return out


def _fetch_groups(
    conn: psycopg.Connection, image_ids: Sequence[int],
) -> dict[int, int | None]:
    out: dict[int, int | None] = {}
    for chunk in _chunks(image_ids):
        with conn.cursor() as cur:
            cur.execute(_IMAGE_GROUPS_SQL, {"image_ids": chunk})
            for image_id, listing_id in cur.fetchall():
                out[int(image_id)] = None if listing_id is None else int(listing_id)
    return out


def assemble_dataset(
    conn: psycopg.Connection, *, tag_id: int,
    encoder: EncoderIdentity | None = None,
    default_target: int = machine_labeling.DEFAULT_TRAINING_TARGET,
) -> DatasetSnapshot:
    """One tag's trainable population, through the two sanctioned doors only.

    `encoder` defaults to whichever of the seven identity facts have the most rows
    stored (`dominant_encoder`); pass it explicitly whenever the run means a
    particular encoder. An image the operator labeled but that has no vector under
    that encoder is REPORTED (`missing_embedding`), never silently dropped: a head
    trained on half its set because the embedding job had not caught up is exactly
    the failure a count in a log line hides.
    """
    positives = machine_labeling.training_set_positive_ids(
        conn, tag_id=tag_id, default_target=default_target)
    negative_rows = tag_holdout.training_label_rows(
        conn, tag_id=tag_id, states=("negative",))

    positive_ids = sorted({int(i) for i in positives})
    positive_set = set(positive_ids)
    # A cell cannot be positive and negative at once; if the doors ever disagree
    # (a machine positive later marked negative by hand, say), the operator's
    # negative is not thrown away silently — it is named and the row is dropped
    # from training entirely rather than guessed at.
    negative_ids: list[int] = []
    conflicting: list[int] = []
    for image_id, _state in negative_rows:
        image_id = int(image_id)
        if image_id in positive_set:
            conflicting.append(image_id)
        else:
            negative_ids.append(image_id)
    conflicting = sorted(set(conflicting))
    conflict_set = set(conflicting)
    positive_ids = [i for i in positive_ids if i not in conflict_set]
    negative_ids = sorted(set(negative_ids))

    if encoder is None:
        encoder = dominant_encoder(conn)
        if encoder is None:
            raise TagHeadError(
                "image_dinov3_embeddings holds no vectors — name an encoder "
                "explicitly or run the embedding job first")

    labels = {i: 1 for i in positive_ids}
    labels.update({i: 0 for i in negative_ids})
    all_ids = sorted(labels)
    if not all_ids:
        raise TagHeadError(f"tag {tag_id} has no trainable labels")

    vectors = _fetch_embeddings(conn, all_ids, encoder)
    groups = _fetch_groups(conn, all_ids)

    rows: list[DatasetRow] = []
    missing_embedding: list[int] = []
    missing_group: list[int] = []
    for image_id in all_ids:
        vec = vectors.get(image_id)
        if vec is None:
            missing_embedding.append(image_id)
            continue
        listing_id = groups.get(image_id)
        if listing_id is None:
            # A singleton group keyed off the image itself: an unattributed image
            # can still train, and a group of one can never leak across the split.
            # Negative so it cannot collide with a real listing_id.
            missing_group.append(image_id)
            listing_id = -image_id
        rows.append(DatasetRow(image_id=image_id, listing_id=listing_id,
                               label=labels[image_id], embedding=vec))

    widths = {len(r.embedding) for r in rows}
    if len(widths) > 1:
        raise TagHeadError(
            f"tag {tag_id}: mixed embedding widths {sorted(widths)} under one "
            "encoder identity — the store is inconsistent")

    return DatasetSnapshot(
        tag_id=int(tag_id), encoder=encoder, rows=tuple(rows),
        dataset_hash=dataset_hash(
            tag_id=tag_id, encoder=encoder,
            rows=[(r.image_id, r.listing_id, r.label) for r in rows]),
        missing_embedding=tuple(missing_embedding),
        missing_group=tuple(missing_group),
        conflicting=tuple(conflicting),
    )


# --- training ---------------------------------------------------------------

@dataclass(frozen=True)
class HeadMetrics:
    """What the head scored, and over how many graded examples.

    `graded_n` is first-class and not optional. A precision of 1.00 over 8 held-out
    examples and one over 400 are different facts, and a bare proportion hides
    which one you have — the point `clip-linear-probe.md` makes with Wilson bounds.
    Every count needed to recompute a bound is here.
    """
    precision: float
    recall: float
    f1: float
    accuracy: float
    graded_n: int
    graded_positive_n: int
    graded_negative_n: int
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int
    n_splits: int
    strategy: str
    group_key: str = "listing_id"

    def as_dict(self) -> dict[str, Any]:
        return {
            "precision": self.precision, "recall": self.recall, "f1": self.f1,
            "accuracy": self.accuracy, "graded_n": self.graded_n,
            "graded_positive_n": self.graded_positive_n,
            "graded_negative_n": self.graded_negative_n,
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "true_negatives": self.true_negatives,
            "false_negatives": self.false_negatives,
            "n_splits": self.n_splits, "strategy": self.strategy,
            "group_key": self.group_key,
        }


@dataclass(frozen=True)
class OofPrediction:
    """One out-of-fold verdict: this row's score from a model that never saw its
    listing."""
    image_id: int
    listing_id: int
    label: int
    score: float
    predicted: int


@dataclass(frozen=True)
class TrainedHead:
    artifact: dict[str, Any]
    metrics: HeadMetrics
    snapshot: DatasetSnapshot
    oof: tuple[OofPrediction, ...]
    estimator: Any = field(repr=False, default=None)   # fitted sklearn model; never serialized


def _fit(X, y, *, C: float, max_iter: int, seed: int):
    from sklearn.linear_model import LogisticRegression

    if len(set(y)) < 2:
        raise TagHeadError(
            "a training fold holds one class only — the grouped split cannot keep "
            "both classes on both sides of this dataset")
    # L2 by default in every supported sklearn; passing penalty="l2" explicitly
    # is deprecated from 1.8 and removed in 1.10, so the default is what keeps one
    # call working across the whole >=1.4 range. The artifact still records the
    # penalty, because a reader of the JSON should not have to know that.
    model = LogisticRegression(
        C=C, solver="lbfgs", class_weight="balanced",
        max_iter=max_iter, random_state=seed,
    )
    model.fit(X, y)
    return model


def _sigmoid(z: float) -> float:
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _metrics(
    oof: Sequence[OofPrediction], *, n_splits: int, strategy: str,
) -> HeadMetrics:
    tp = sum(1 for p in oof if p.label == 1 and p.predicted == 1)
    fp = sum(1 for p in oof if p.label == 0 and p.predicted == 1)
    tn = sum(1 for p in oof if p.label == 0 and p.predicted == 0)
    fn = sum(1 for p in oof if p.label == 1 and p.predicted == 0)
    # Pooled over ALL folds, not averaged per fold: a fold that happens to hold no
    # positives has an undefined precision, and averaging undefined-per-fold numbers
    # invents a result. One confusion matrix over every graded row has none of that.
    # 0.0 for an empty denominator (sklearn's zero_division=0): a head that predicts
    # nothing positive scored nothing, and 1.0 would read as perfect.
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    total = len(oof)
    return HeadMetrics(
        precision=precision, recall=recall, f1=f1,
        accuracy=(tp + tn) / total if total else 0.0,
        graded_n=total,
        graded_positive_n=tp + fn, graded_negative_n=tn + fp,
        true_positives=tp, false_positives=fp,
        true_negatives=tn, false_negatives=fn,
        n_splits=n_splits, strategy=strategy,
    )


def grouped_folds(
    snapshot: DatasetSnapshot, *, n_splits: int = DEFAULT_N_SPLITS,
    seed: int = DEFAULT_SEED,
) -> list[tuple[list[int], list[int]]]:
    """The grouped CV folds as row-index pairs, keyed on listing_id.

    Separate from `train_head` so the property that matters can be asserted
    directly: no listing's images appear on both sides of a fold. A naive random
    split lets a head score by recognizing the rest of a listing it trained on,
    which reads as accuracy and is not.
    """
    from sklearn.model_selection import StratifiedGroupKFold

    rows = snapshot.rows
    y = [r.label for r in rows]
    groups = [r.listing_id for r in rows]
    pos_groups = {r.listing_id for r in rows if r.label == 1}
    neg_groups = {r.listing_id for r in rows if r.label == 0}
    usable = min(n_splits, len(pos_groups), len(neg_groups))
    if usable < 2:
        raise TagHeadError(
            f"tag {snapshot.tag_id}: {len(pos_groups)} positive and "
            f"{len(neg_groups)} negative listing-groups — fewer than 2 of either "
            "cannot be graded on a grouped split, and an ungraded head is not a "
            "result. Label more, or label across more listings.")
    splitter = StratifiedGroupKFold(n_splits=usable, shuffle=True, random_state=seed)
    X = [list(r.embedding) for r in rows]
    return [(list(tr), list(ev)) for tr, ev in splitter.split(X, y, groups)]


def train_head(
    snapshot: DatasetSnapshot, *, trained_at: datetime,
    n_splits: int = DEFAULT_N_SPLITS, C: float = DEFAULT_C,
    threshold: float = DEFAULT_THRESHOLD, max_iter: int = DEFAULT_MAX_ITER,
    seed: int = DEFAULT_SEED,
) -> TrainedHead:
    """Fit one binary head and grade it out-of-fold on grouped folds.

    GROUPED BY listing_id (D4.2): a listing's photos never straddle a fold, so the
    head cannot score by recognizing the rest of a listing it already saw. The
    strategy is StratifiedGroupKFold rather than one held-out split because the
    binding resource is operator label-days: k-fold grades EVERY labeled example
    exactly once out-of-fold, so `graded_n` equals the whole set instead of the
    ~20% a single split would leave. The shipped weights are then refit on all
    rows — the standard split of duties between "how good is this recipe" (CV) and
    "the model you deploy" (refit).

    `trained_at` is a parameter, not a clock read: the same snapshot and the same
    timestamp must produce the same artifact bytes.
    """
    import sklearn

    rows = snapshot.rows
    if not rows:
        raise TagHeadError(f"tag {snapshot.tag_id}: nothing to train on")
    X = [list(r.embedding) for r in rows]
    y = [r.label for r in rows]

    folds = grouped_folds(snapshot, n_splits=n_splits, seed=seed)
    oof: list[OofPrediction] = []
    for train_idx, eval_idx in folds:
        fold = _fit([X[i] for i in train_idx], [y[i] for i in train_idx],
                    C=C, max_iter=max_iter, seed=seed)
        scores = fold.decision_function([X[i] for i in eval_idx])
        for i, raw in zip(eval_idx, scores):
            p = _sigmoid(float(raw))
            oof.append(OofPrediction(
                image_id=rows[i].image_id, listing_id=rows[i].listing_id,
                label=rows[i].label, score=p, predicted=1 if p >= threshold else 0))
    oof.sort(key=lambda p: p.image_id)

    metrics = _metrics(oof, n_splits=len(folds), strategy="StratifiedGroupKFold")
    final = _fit(X, y, C=C, max_iter=max_iter, seed=seed)

    artifact = {
        "artifact_version": ARTIFACT_VERSION,
        "kind": ARTIFACT_KIND,
        "tag_id": int(snapshot.tag_id),
        "encoder": snapshot.encoder.as_dict(),
        "dataset_hash": snapshot.dataset_hash,
        "trained_at": trained_at.isoformat(),
        "dimension": snapshot.dimension,
        "n_positive": snapshot.n_positive,
        "n_negative": snapshot.n_negative,
        "n_groups": snapshot.n_groups,
        "threshold": float(threshold),
        "hyperparameters": {
            "penalty": "l2", "C": float(C), "solver": "lbfgs",
            "class_weight": "balanced", "max_iter": int(max_iter), "seed": int(seed),
        },
        "metrics": metrics.as_dict(),
        "weights": [float(w) for w in final.coef_[0]],
        "bias": float(final.intercept_[0]),
        "sklearn_version": sklearn.__version__,
    }
    return TrainedHead(artifact=artifact, metrics=metrics, snapshot=snapshot,
                       oof=tuple(oof), estimator=final)


# --- the artifact, and inference from it without sklearn --------------------

_REQUIRED_ARTIFACT_KEYS = (
    "artifact_version", "kind", "tag_id", "encoder", "dataset_hash", "trained_at",
    "threshold", "metrics", "weights", "bias",
)


def save_artifact(artifact: dict[str, Any], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


def load_artifact(path: str | Path) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [k for k in _REQUIRED_ARTIFACT_KEYS if k not in raw]
    if missing:
        raise TagHeadError(f"artifact is missing {', '.join(missing)}")
    if raw["kind"] != ARTIFACT_KIND:
        raise TagHeadError(f"not a tag head artifact: kind={raw['kind']!r}")
    if int(raw["artifact_version"]) != ARTIFACT_VERSION:
        raise TagHeadError(
            f"artifact version {raw['artifact_version']} != {ARTIFACT_VERSION}")
    return raw


def artifact_encoder(artifact: dict[str, Any]) -> EncoderIdentity:
    """The encoder the head was fit against. Scoring a vector produced by any other
    is meaningless — the caller checks, this is what it checks against."""
    return EncoderIdentity.from_dict(artifact["encoder"])


def score_embedding(artifact: dict[str, Any], embedding: Sequence[float]) -> float:
    """P(tag) for one vector, in pure Python.

    No numpy, no sklearn: a dot product and a logistic. `clip-linear-probe.md` D5
    keeps scikit-learn to a training-only extra; this is what makes that true — an
    inference-side caller needs the JSON and nothing else.
    """
    weights = artifact["weights"]
    if len(embedding) != len(weights):
        raise TagHeadError(
            f"embedding has {len(embedding)} dims, head expects {len(weights)}")
    z = float(artifact["bias"])
    for w, x in zip(weights, embedding):
        z += float(w) * float(x)
    return _sigmoid(z)


def predict(artifact: dict[str, Any], embedding: Sequence[float]) -> bool:
    return score_embedding(artifact, embedding) >= float(artifact["threshold"])
