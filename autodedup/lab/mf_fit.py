"""`lab mf-fit`: the challenger's model files, fitted SEALED BY TOWN (G2 `sealed.oof`, B-b).

The training data is one GROUND per cohort, built by `lab ground` (`autodedup/lab/ground.py`; any
other file is refused): its labelled pairs, each with its feature row, label, weight, origin and the
towns of its two adverts, and the town sets its candidates span. A model belongs to a TOWN SET (one
town, or the two towns of a cross-town pair, `challenger.town_set`): it trains on every kept label
of every ground that touches no advert of any town in the set, whichever ground holds it, and scores
only the pairs that span exactly that set. Each ground's labelled rows are scored by their own set's
model, and those out-of-set raw scores fit the isotonic map: `loco` fits one map per scored cohort on every OTHER ground's kept rows
(GLOBAL_SEARCH 1.4 / 2.1: never on the cohort it scores); `trial` fits one on the trial's rows (G2's
s6) and `pooled` one on every ground's kept rows (G2's s5), both kept for the G2-compatible rows and
both reading the scored cohort's own labels. Each model is written as the JSON file
`autodedup.challenger.score` evaluates in pure Python; the fit re-scores every ground row through
that evaluation, and the card records the largest difference from scikit-learn (<= 1e-9 or the
file is not written). A ground holding a block a preregistration seals is refused unless the
registry names that cohort's freeze. scikit-learn is the training extra, imported only here."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from autodedup import evidence
from autodedup.challenger.score import FORMAT, scorer
from autodedup.lab import ground as lab_ground
from autodedup.lab.challenger import town_set

PARAMS: dict[str, Any] = {"max_iter": 400, "learning_rate": 0.05, "max_leaf_nodes": 31,
                          "min_samples_leaf": 20, "l2_regularization": 1.0,
                          "random_state": 20260927}
JUDGE_ORIGINS: tuple[str, ...] = ("judge_trial", "w6")
SOURCES: dict[str, Callable[[np.ndarray], np.ndarray]] = {
    "all": lambda origin: np.ones(len(origin), dtype=bool),
    "judge": lambda origin: np.isin(origin, JUDGE_ORIGINS),
}
TOLERANCE: float = 1e-9
CALIBRATIONS: tuple[str, ...] = ("loco", "trial", "pooled")
BLOCK = re.compile(r"^([a-z_]+?)(\d+)$")


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
    spans: list[str]
    feature_order: list[str]
    meta: dict[str, Any]

    @property
    def sets(self) -> np.ndarray:
        """Each labelled row's town set."""
        return np.array([town_set(a, b) for a, b in self.blocks.tolist()], dtype=object)


class NotALabGround(ValueError):
    """A training file `lab ground` did not write, or one edited since."""


def load_ground(name: str, path: str | Path) -> Ground:
    """A ground `lab ground` wrote for cohort `name`, its digest re-checked."""
    data = np.load(path, allow_pickle=False)
    meta = json.loads(str(data["meta"])) if "meta" in data.files else {}
    if meta.get("builder") != lab_ground.BUILDER or meta.get("cohort") != name:
        raise NotALabGround(f"{path}: not a `lab ground` file of cohort {name} ({meta or 'no meta'})")
    if lab_ground.digest({k: data[k] for k in data.files}) != meta.get("digest"):
        raise NotALabGround(f"{path}: its rows are not the ones `lab ground` wrote")
    if "spans" not in data.files:
        raise NotALabGround(f"{path}: written before grounds carried their town sets; rebuild it")
    return Ground(name, data["keys"].astype(np.int64), data["V"].astype(np.float64),
                  data["P"].astype(bool), data["y"].astype(np.int64), data["w"].astype(np.float64),
                  data["origin"].astype(str), data["blocks"].astype(str),
                  [str(t) for t in data["towns"]], [str(t) for t in data["spans"]],
                  [str(f) for f in data["feature_order"]], meta)


def check_seals(grounds: Sequence[Ground], reg: Mapping[str, Any]) -> None:
    """Refuse a ground holding a block a preregistration seals (committed, `$AUTODEDUP_SEALS` or
    the registry's `seals`), unless the registry names that cohort's freeze."""
    sealed = evidence.sealed_blocks(evidence.seal_files(reg.get("seals") or ()))
    for g in grounds:
        names = set(g.towns) | set(g.blocks.ravel().tolist())
        spelled = {f"{m.group(1)}:{int(m.group(2))}" for m in map(BLOCK.match, names) if m}
        hit = sorted(spelled & set(sealed))
        if hit and not (reg.get("cohorts") or {}).get(g.name, {}).get("freeze"):
            raise evidence.SealedExport(
                f"ground {g.name} holds sealed blocks {hit} ({sorted({sealed[b] for b in hit})}); "
                "a sealed cohort is read once, at its freeze")
    settings = {g.meta.get("settings") for g in grounds}
    if len(settings) > 1:
        raise ValueError(f"the grounds were featurised under {len(settings)} settings rows: "
                         "two settings rows are two feature definitions")


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
    if not len(X):
        return 0.0
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


def _touching(g: Ground, key: str) -> np.ndarray:
    towns = key.split("+")
    return np.isin(g.blocks[:, 0], towns) | np.isin(g.blocks[:, 1], towns)


def sealed(grounds: Sequence[Ground], scored: Sequence[str], source: str, calibration: str,
           out: Path) -> dict[str, Any]:
    """Every town and every town set a candidate or a label spans, of every `scored` cohort, gets a
    model file under `out/<source>/<cohort>/`; the report says what each one trained on, which map
    it carries and how far its evaluation is from scikit-learn."""
    import sklearn

    if calibration not in CALIBRATIONS:
        raise ValueError(f"calibration must be one of {CALIBRATIONS}: {calibration}")
    clock = time.perf_counter()
    keep = SOURCES[source]
    masks = {g.name: keep(g.origin) for g in grounds}
    by_name = {g.name: g for g in grounds}
    fitted: dict[tuple[Any, ...], tuple[Fitted, dict[str, Any]]] = {}

    def model_for(key: str) -> tuple[Any, ...]:
        """The town set's model: every kept row of every ground that touches a town of it is out."""
        out_rows = [masks[h.name] & _touching(h, key) for h in grounds]
        key = tuple((h.name, tuple(np.flatnonzero(m).tolist()))
                    for h, m in zip(grounds, out_rows) if m.any())
        if key not in fitted:
            use = [masks[h.name] & ~m for h, m in zip(grounds, out_rows)]
            X = np.vstack([design(h.V[u], h.P[u]) for h, u in zip(grounds, use)])
            y = np.concatenate([h.y[u] for h, u in zip(grounds, use)])
            w = np.concatenate([h.w[u] for h, u in zip(grounds, use)])
            origins = np.concatenate([h.origin[u] for h, u in zip(grounds, use)])
            trained = {"rows": int(len(y)), "pos": int(y.sum()), "neg": int((y == 0).sum()),
                       "by_origin": dict(sorted(Counter(origins.tolist()).items())),
                       "grounds": {h.name: int(u.sum()) for h, u in zip(grounds, use)},
                       "excluded": {h.name: int(m.sum()) for h, m in zip(grounds, out_rows)},
                       "labels": _digest(grounds, use)}
            fitted[key] = (fit_model(X, y, w), trained)
        return key

    raw: dict[str, np.ndarray] = {}

    def raw_of(g: Ground) -> np.ndarray:
        """Every labelled row of `g` scored by its own town set's sealed model."""
        if g.name not in raw:
            column = np.full(len(g.y), np.nan)
            sets = g.sets
            for key in sorted(set(sets.tolist())):
                rows = sets == key
                column[rows] = fitted[model_for(key)][0].raw(design(g.V[rows], g.P[rows]))
            raw[g.name] = column
        return raw[g.name]

    sets_of = {name: [(key, model_for(key)) for key in
                      sorted(set(by_name[name].towns) | set(by_name[name].spans)
                             | set(by_name[name].sets.tolist()))]
               for name in scored}
    maps: dict[str, tuple[Any, dict[str, Any]]] = {}
    for name in scored:
        if calibration == "trial":
            base = [(by_name["trial"], np.ones(len(by_name["trial"].y), dtype=bool))]
        elif calibration == "pooled":
            base = [(g, masks[g.name]) for g in grounds if masks[g.name].any()]
        else:
            base = [(g, masks[g.name]) for g in grounds if g.name != name and masks[g.name].any()]
        if not base:
            raise ValueError(f"{name}: no ground outside it holds a {source} label to fit its map")
        xs = np.concatenate([raw_of(g)[m] for g, m in base])
        ys = np.concatenate([g.y[m] for g, m in base])
        iso = isotonic(xs, ys, np.concatenate([g.w[m] for g, m in base]))
        maps[name] = (iso, {
            "kind": "isotonic", "mode": calibration, "rows": int(len(xs)), "pos": int(ys.sum()),
            "neg": int((ys == 0).sum()), "cohorts": [g.name for g, _ in base],
            "labels": "all" if calibration == "trial" else source,
            "reads_the_scored_cohort": any(g.name == name for g, _ in base),
            "note": {"loco": "every other ground's kept rows, each scored by its own town set's "
                             "sealed model; no row of the scored cohort",
                     "trial": "the trial's labelled rows, each scored by its own town set's sealed "
                              "model (G2 s6's rows)",
                     "pooled": "every ground's kept rows, each scored by its own town set's sealed "
                               "model (G2 s5's rows)"}[calibration]})
    report: dict[str, Any] = {"source": source, "calibration": calibration,
                              "grounds": {g.name: g.meta for g in grounds}, "maps": {},
                              "cohorts": {}}
    for name in scored:
        g = by_name[name]
        iso, fitted_on = maps[name]
        report["maps"][name] = fitted_on
        sets = g.sets
        for tset, key in sets_of[name]:
            model, trained = fitted[key]
            card = {"source": source, "cohort": name, "town_set": tset,
                    "sealed": f"no label touching an advert of {' or '.join(tset.split('+'))}, "
                              "in any ground, trains it",
                    "trained_on": trained, "learner": "sklearn.HistGradientBoostingClassifier",
                    "params": PARAMS, "sklearn": sklearn.__version__,
                    "sample_weight": "the label's weight, class mass equalised",
                    "grounds": {h.name: h.meta.get("digest") for h in grounds},
                    "calibration": fitted_on, "exporter": FORMAT}
            spec = export(model, g.feature_order, iso, card)
            rows = sets == tset
            on = tset if rows.any() else f"every labelled row of {name} (none spans {tset})"
            rows = rows if rows.any() else np.ones(len(sets), dtype=bool)
            X = design(g.V[rows], g.P[rows])
            gap = equivalence(spec, model, iso, X)
            if gap > TOLERANCE:
                raise AssertionError(f"{name}/{tset}: pure Python differs from scikit-learn by {gap}")
            spec["card"]["equivalence"] = {"rows": int(rows.sum()), "rows_of": on,
                                           "max_abs_dp": gap, "tolerance": TOLERANCE}
            path = out / source / name / f"{tset}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(spec, separators=(",", ":")), encoding="utf-8")
            tmp.replace(path)
            report["cohorts"].setdefault(name, {})[tset] = {
                "file": str(path), "trained_on": trained, "equivalence_rows": int(rows.sum()),
                "max_abs_dp": gap}
    report["models_fitted"] = len(fitted)
    report["wall_s"] = round(time.perf_counter() - clock, 1)
    return report
