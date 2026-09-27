"""`lab mf-fit`: the challenger's model files, fitted SEALED BY TOWN (G2 `sealed.oof`, B-b).

The training data is one GROUND per cohort (`load_ground`): its labelled pairs, each with its
feature row, label, weight, origin and the towns of its two adverts. A town's model trains on every
kept label of every ground that touches no advert of that town, and scores only the pairs whose lo
advert sits in it. Each ground's own labelled rows are scored by their own town's model, and those
out-of-town raw scores fit the isotonic map: `trial` fits it on the trial's rows (G2's s6),
`pooled` on every ground's kept rows (G2's s5). Each model is written as the JSON file
`autodedup.challenger.score` evaluates in pure Python; the fit re-scores every ground row through
that evaluation, and the card records the largest difference from scikit-learn (<= 1e-9 or the
file is not written). scikit-learn is the training extra, imported only here."""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from autodedup.challenger.score import FORMAT, scorer

PARAMS: dict[str, Any] = {"max_iter": 400, "learning_rate": 0.05, "max_leaf_nodes": 31,
                          "min_samples_leaf": 20, "l2_regularization": 1.0,
                          "random_state": 20260927}
JUDGE_ORIGINS: tuple[str, ...] = ("judge_trial", "w6")
SOURCES: dict[str, Callable[[np.ndarray], np.ndarray]] = {
    "all": lambda origin: np.ones(len(origin), dtype=bool),
    "judge": lambda origin: np.isin(origin, JUDGE_ORIGINS),
}
TOLERANCE: float = 1e-9


@dataclass
class Ground:
    """One cohort's labelled pairs as training rows (labels in their ground order)."""

    name: str
    keys: np.ndarray
    V: np.ndarray
    P: np.ndarray
    y: np.ndarray
    w: np.ndarray
    origin: np.ndarray
    blocks: np.ndarray
    towns: list[str]
    feature_order: list[str]


def load_ground(name: str, path: str | Path) -> Ground:
    data = np.load(path, allow_pickle=False)
    return Ground(name, data["keys"].astype(np.int64), data["V"].astype(np.float64),
                  data["P"].astype(bool), data["y"].astype(np.int64), data["w"].astype(np.float64),
                  data["origin"].astype(str), data["blocks"].astype(str),
                  [str(t) for t in data["towns"]], [str(f) for f in data["feature_order"]])


def design(V: np.ndarray, P: np.ndarray) -> np.ndarray:
    return np.where(P, V, np.nan)


def balanced(y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Positive and negative weight mass equalised (G2 `learn.balanced`)."""
    pos, neg = w[y == 1].sum(), w[y == 0].sum()
    out = w.copy()
    out[y == 1] *= 0.5 * (pos + neg) / max(pos, 1e-9)
    out[y == 0] *= 0.5 * (pos + neg) / max(neg, 1e-9)
    return out


@dataclass
class Fitted:
    clf: Any
    inputs: list[int]
    fill: dict[int, float]

    def tree_input(self, X: np.ndarray) -> np.ndarray:
        X = X.copy()
        for j, value in self.fill.items():
            X[np.isnan(X[:, j]), j] = value
        return X[:, self.inputs]

    def raw(self, X: np.ndarray) -> np.ndarray:
        return self.clf.predict_proba(self.tree_input(X))[:, 1]


def fit_model(X: np.ndarray, y: np.ndarray, w: np.ndarray, params: Mapping[str, Any] = PARAMS
              ) -> Fitted:
    """G2's boosting (`learn.Model('hgb')`): a column no row states is not an input; a column with
    one value when present splits on its presence (missing reads one below it)."""
    from sklearn.ensemble import HistGradientBoostingClassifier

    inputs, fill = [], {}
    for j in range(X.shape[1]):
        seen = np.unique(X[~np.isnan(X[:, j]), j])
        if len(seen) == 0:
            continue
        inputs.append(j)
        if len(seen) == 1:
            fill[j] = float(seen[0]) - 1.0
    fitted = Fitted(HistGradientBoostingClassifier(**params), inputs, fill)
    fitted.clf.fit(fitted.tree_input(X), y, sample_weight=balanced(y, w))
    return fitted


def isotonic(raw: np.ndarray, y: np.ndarray, w: np.ndarray) -> Any:
    from sklearn.isotonic import IsotonicRegression

    return IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(raw, y,
                                                                              sample_weight=w)


def export(fitted: Fitted, feature_order: Sequence[str], iso: Any | None,
           card: Mapping[str, Any]) -> dict[str, Any]:
    """The fitted classifier as the challenger's model file (`challenger.score`)."""
    clf = fitted.clf
    if clf.n_trees_per_iteration_ != 1 or getattr(clf, "_preprocessor", None) is not None:
        raise ValueError("only a binary model on numerical features exports")
    trees = []
    for (predictor,) in clf._predictors:
        nodes = predictor.nodes
        if nodes["is_categorical"].any():
            raise ValueError("a categorical split does not export")
        trees.append([[int(n["feature_idx"]), float(n["num_threshold"]),
                       bool(n["missing_go_to_left"]), int(n["left"]), int(n["right"]),
                       float(n["value"]), bool(n["is_leaf"])] for n in nodes])
    calibration = None
    if iso is not None:
        calibration = {"x": [float(v) for v in iso.X_thresholds_],
                       "y": [float(v) for v in iso.y_thresholds_],
                       "lo": float(iso.X_min_), "hi": float(iso.X_max_)}
    return {"format": FORMAT, "feature_order": list(feature_order), "inputs": fitted.inputs,
            "fill": {str(j): v for j, v in fitted.fill.items()},
            "baseline": float(clf._baseline_prediction.ravel()[0]), "trees": trees,
            "calibration": calibration, "card": dict(card)}


def equivalence(model: Mapping[str, Any], fitted: Fitted, iso: Any | None, X: np.ndarray
                ) -> float:
    """The largest |p| difference between the pure-Python evaluation and scikit-learn."""
    want = fitted.raw(X)
    if iso is not None:
        want = iso.predict(want)
    p = scorer(model)
    got = np.array([p([None if v != v else float(v) for v in row]) for row in X.tolist()])
    return float(np.max(np.abs(got - want))) if len(X) else 0.0


def _digest(g: Sequence[Ground], masks: Sequence[np.ndarray]) -> str:
    body = [[h.name, h.keys[m].tolist(), h.y[m].tolist(), [round(float(x), 6) for x in h.w[m]]]
            for h, m in zip(g, masks)]
    return hashlib.sha1(json.dumps(body).encode()).hexdigest()[:12]


def sealed(grounds: Sequence[Ground], scored: Sequence[str], source: str, calibration: str,
           out: Path) -> dict[str, Any]:
    """Every town of every `scored` cohort gets a model file under `out/<source>/<cohort>/`; the
    report says what each one trained on and how far its evaluation is from scikit-learn."""
    import sklearn

    clock = time.perf_counter()
    keep = SOURCES[source]
    masks = {g.name: keep(g.origin) for g in grounds}
    by_name = {g.name: g for g in grounds}
    fitted: dict[tuple[Any, ...], tuple[Fitted, dict[str, Any]]] = {}
    raw = {g.name: np.full(len(g.y), np.nan) for g in grounds}
    towns_of: dict[str, list[tuple[str, tuple[Any, ...]]]] = {}
    for name in scored:
        g = by_name[name]
        towns_of[name] = []
        for town in sorted(set(g.towns) | set(g.blocks[:, 0].tolist())):
            touch = masks[name] & ((g.blocks[:, 0] == town) | (g.blocks[:, 1] == town))
            key = (name, tuple(np.flatnonzero(touch).tolist())) if touch.any() else ()
            if key not in fitted:
                use = [masks[h.name] & ~touch if h.name == name else masks[h.name]
                       for h in grounds]
                X = np.vstack([design(h.V[u], h.P[u]) for h, u in zip(grounds, use)])
                y = np.concatenate([h.y[u] for h, u in zip(grounds, use)])
                w = np.concatenate([h.w[u] for h, u in zip(grounds, use)])
                origins = np.concatenate([h.origin[u] for h, u in zip(grounds, use)])
                trained = {"rows": int(len(y)), "pos": int(y.sum()), "neg": int((y == 0).sum()),
                           "by_origin": dict(sorted(Counter(origins.tolist()).items())),
                           "grounds": {h.name: int(u.sum()) for h, u in zip(grounds, use)},
                           "labels": _digest(grounds, use)}
                fitted[key] = (fit_model(X, y, w), trained)
            towns_of[name].append((town, key))
            rows = g.blocks[:, 0] == town
            if rows.any():
                raw[name][rows] = fitted[key][0].raw(design(g.V[rows], g.P[rows]))
    if calibration == "trial":
        base = [(by_name["trial"], np.ones(len(by_name["trial"].y), dtype=bool))]
    elif calibration == "pooled":
        base = [(g, masks[g.name]) for g in grounds if masks[g.name].any()]
    else:
        raise ValueError(f"calibration must be trial or pooled: {calibration}")
    unscored = sorted(g.name for g, _ in base if g.name not in scored)
    if unscored:
        raise ValueError(f"the map reads rows of {unscored}: score those cohorts too")
    xs = np.concatenate([raw[g.name][m] for g, m in base])
    if np.isnan(xs).any():
        raise ValueError("a calibration row has no out-of-town raw score")
    iso = isotonic(xs, np.concatenate([g.y[m] for g, m in base]),
                   np.concatenate([g.w[m] for g, m in base]))
    fitted_on = {"kind": "isotonic", "rows": int(len(xs)), "cohorts": [g.name for g, _ in base],
                 "labels": source if calibration == "pooled" else "all",
                 "note": ("the trial's labelled rows, each scored by its own town's sealed model "
                          "(G2 s6)" if calibration == "trial" else
                          "every scored ground's kept rows, each scored by its own town's sealed "
                          "model (G2 s5 pooled)")}
    report: dict[str, Any] = {"source": source, "calibration": fitted_on, "cohorts": {}}
    for name in scored:
        g = by_name[name]
        for town, key in towns_of[name]:
            model, trained = fitted[key]
            card = {"source": source, "cohort": name, "town": town,
                    "sealed": f"no label touching an advert of {town} (cohort {name}) trains it",
                    "trained_on": trained, "learner": "sklearn.HistGradientBoostingClassifier",
                    "params": PARAMS, "sklearn": sklearn.__version__,
                    "sample_weight": "the label's weight, class mass equalised",
                    "calibration": fitted_on, "exporter": FORMAT}
            spec = export(model, g.feature_order, iso, card)
            rows = g.blocks[:, 0] == town
            X = design(g.V[rows], g.P[rows])
            gap = equivalence(spec, model, iso, X)
            if gap > TOLERANCE:
                raise AssertionError(f"{name}/{town}: pure Python differs from scikit-learn by {gap}")
            spec["card"]["equivalence"] = {"rows": int(rows.sum()), "max_abs_dp": gap,
                                           "tolerance": TOLERANCE}
            path = out / source / name / f"{town}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(spec, separators=(",", ":")), encoding="utf-8")
            tmp.replace(path)
            report["cohorts"].setdefault(name, {})[town] = {
                "file": str(path), "trained_on": trained, "equivalence_rows": int(rows.sum()),
                "max_abs_dp": gap}
    report["models_fitted"] = len(fitted)
    report["wall_s"] = round(time.perf_counter() - clock, 1)
    return report
