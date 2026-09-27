"""`lab fit`: the standing challenger's learner (G3's gradient-boosted model) fitted ON THE ONE PATH.

It trains on the train cohort's artefact (its own V/P, its own keys) and the registry's labels
(the judge labels as training data, the operator's rulings over them), cross-fitted by label
component on the train cohort (out-of-fold probabilities for its labelled pairs), refitted without
a scored cohort's own ruled pairs for every other cohort, and writes one `.npz` per cohort keyed on
exactly that artefact's pairs and naming its version, which `score.external_scores` requires. Each
file is recorded in the cache root's `fits.jsonl`, so a leaderboard row can say its scorer came from
here and from which cache. scikit-learn is the `training` extra (imported only here)."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from autodedup.evaluate import SAME
from autodedup.lab.cache import Cohort
from autodedup.lab.metrics import Labels
from autodedup.lab.score import file_sha

FITS_FILE: str = "fits.jsonl"
FOLDS: int = 5
LEARNER: dict[str, Any] = {"max_iter": 300, "learning_rate": 0.05, "max_leaf_nodes": 15,
                           "min_samples_leaf": 10, "l2_regularization": 1.0, "random_state": 7}
JUDGE_ORDER: tuple[str, ...] = ("text", "vision", "gold")


def matrix(c: Cohort) -> np.ndarray:
    """The artefact's feature vector with ABSENT as NaN (the learner's missing value)."""
    return np.where(c.P, c.V, np.nan).astype(np.float32)


def trainable(X: np.ndarray) -> np.ndarray:
    """The training rows with a column no row states set to 0.0: the learner's binning refuses an
    all-missing column, and a constant one is never split on, so the fitted model is the same."""
    empty = np.isnan(X).all(axis=0)
    if not empty.any():
        return X
    out = X.copy()
    out[:, empty] = 0.0
    return out


def training_rows(keys: list[tuple[int, int]], labels: Labels,
                  exclude: frozenset[tuple[int, int]] = frozenset()
                  ) -> list[tuple[int, int, tuple[int, int]]]:
    """(row index, 1 = same, key) for every labelled candidate pair; a ruling beats a judge."""
    y: dict[tuple[int, int], str] = {}
    for source in JUDGE_ORDER:
        y.update(labels.judges.get(source, {}))
    y.update(labels.rulings)
    index = {k: i for i, k in enumerate(keys)}
    return [(index[k], 1 if v == SAME else 0, k) for k, v in sorted(y.items())
            if k in index and k not in exclude]


def components(pairs: Iterable[tuple[int, int]]) -> list[int]:
    """Each pair's label component (the union of its adverts), the cross-fitting group."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    pairs = list(pairs)
    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    return [find(a) for a, _ in pairs]


def _digest(rows: list[tuple[int, int, tuple[int, int]]]) -> str:
    return hashlib.sha1(json.dumps([[k[0], k[1], y] for _, y, k in rows]).encode()).hexdigest()[:12]


def write_npz(path: Path, c: Cohort, p: np.ndarray, meta: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.npz")
    np.savez(tmp, lo=c.lo, hi=c.hi, p=p.astype(np.float64),
             meta=np.array(json.dumps({**meta, "cache": c.version, "cohort": c.name},
                                      sort_keys=True)))
    tmp.replace(path)
    return file_sha(path)


def fit(train: Cohort, others: Iterable[Cohort], labels: Labels, out: Path,
        root: Path | None = None) -> dict[str, Any]:
    """Fit on `train`, score `train` (out-of-fold on its labelled pairs) and each of `others`,
    write `gbm_<cohort>.npz` under `out` and record each in `root/fits.jsonl`."""
    import sklearn
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold

    def model() -> Any:
        return HistGradientBoostingClassifier(**LEARNER)
    clock = time.perf_counter()
    X = matrix(train)
    rows = training_rows(train.keys, labels)
    idx = np.array([r[0] for r in rows], dtype=np.int64)
    y = np.array([r[1] for r in rows], dtype=np.int64)
    groups = np.array(components(r[2] for r in rows))
    full = model().fit(trainable(X[idx]), y)
    p = full.predict_proba(X)[:, 1]
    oof = np.zeros(len(idx))
    for tr, te in GroupKFold(n_splits=FOLDS).split(idx, y, groups):
        oof[te] = model().fit(trainable(X[idx[tr]]), y[tr]).predict_proba(X[idx[te]])[:, 1]
    p[idx] = oof
    base = {"fit": "lab fit", "learner": "sklearn.HistGradientBoostingClassifier",
            "params": LEARNER, "sklearn": sklearn.__version__}
    train_meta = {"cohort": train.name, "cache": train.version, "labelled": len(rows),
                  "same": int(y.sum()), "labels": _digest(rows)}
    report: dict[str, Any] = {train.name: {
        **train_meta, "oof_auc": round(float(roc_auc_score(y, oof)), 4) if 0 < y.sum() < len(y)
        else None}}
    written = [(train, write_npz(out / f"gbm_{train.name}.npz", train, p,
                                 {**base, "train": {**train_meta, "out_of_fold": True}}))]
    for c in others:
        held = frozenset(k for k in labels.rulings if k in set(c.keys))
        kept = training_rows(train.keys, labels, held)
        if not kept:
            raise ValueError(f"no training label is left once {c.name}'s own rulings are held out")
        if len(kept) == len(rows):
            learner = full
        else:
            ix = np.array([r[0] for r in kept], dtype=np.int64)
            learner = model().fit(trainable(X[ix]), np.array([r[1] for r in kept], dtype=np.int64))
        meta = {**base, "train": {**train_meta, "labelled": len(kept), "labels": _digest(kept),
                                  "held_out": len(held)}}
        written.append((c, write_npz(out / f"gbm_{c.name}.npz", c,
                                     learner.predict_proba(matrix(c))[:, 1], meta)))
        report[c.name] = {"pairs": c.n, "ruled_pairs_held_out": len(held),
                          "dropped_from_training": len(rows) - len(kept)}
    if root is not None:
        record(root, [{"sha": sha, "cohort": c.name, "cache": c.version,
                       "path": str(out / f"gbm_{c.name}.npz"), "train": train_meta}
                      for c, sha in written])
    report["files"] = {c.name: sha for c, sha in written}
    report["wall_s"] = round(time.perf_counter() - clock, 1)
    return report


def record(root: Path, entries: Iterable[dict[str, Any]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with open(root / FITS_FILE, "a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps({**entry, "at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                    sort_keys=True) + "\n")


def recorded(root: Path) -> dict[str, dict[str, Any]]:
    """The score files `lab fit` wrote, by content sha."""
    path = root / FITS_FILE
    if not path.is_file():
        return {}
    return {entry["sha"]: entry for entry in
            (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip())}


def scorer_state(scorer: dict[str, Any], cache: str, fits: dict[str, dict[str, Any]]) -> bool:
    """Is the row's score column the one path's? The cache's own score or a model file read on
    it, or a `lab fit` file recorded against this very cache."""
    if scorer.get("kind") != "npz":
        return True
    entry = fits.get(str(scorer.get("sha")))
    return bool(entry) and entry["cache"] == cache == scorer.get("cache")
