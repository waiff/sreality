"""The learned pair score: gradient boosting, a small MLP and a refitted logistic + isotonic, beside
the shipped w6_gold. Inputs are the engine's 60 existing features plus four G1 image-retrieval
slots (SSCD copy score, DINOv3 cosine, LightGlue inliers, same-room matched frames) that are
ABSENT today and enter as missing values, so a G1 side file fills them without a code change."""
from __future__ import annotations

import json
import pickle
import warnings
from dataclasses import dataclass

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler

from autodedup.features import FEATURE_ORDER
from autodedup.model import CALIBRATION_TIE_BREAK

from autodedup_w15_g2.ladder import MODEL
from autodedup_w15_g2.paths import OUT

G1_SLOTS: tuple[str, ...] = ("g1_sscd_max", "g1_dinov3_max", "g1_lightglue_inliers",
                             "g1_room_matched_frames")
FEATURES: tuple[str, ...] = tuple(FEATURE_ORDER) + G1_SLOTS


@dataclass
class Cohort:
    name: str
    keys: list[tuple[int, int]]        # candidates first, then labelled pairs outside retrieval
    n_cand: int
    V: np.ndarray
    P: np.ndarray
    recs: dict
    labels: dict
    ladder: dict
    fired: list | None = None
    reader_names: list | None = None

    @property
    def cand_keys(self) -> list[tuple[int, int]]:
        return self.keys[: self.n_cand]

    def row_block(self) -> list[str]:
        return [self.recs[a].block if self.recs[a].block == self.recs[b].block
                else f"{self.recs[a].block}|{self.recs[b].block}" for a, b in self.keys]


READER_FAMILY: dict[str, str] = {}
for _fam, _names in (
        ("ATTR", "category_type category_main area stated_area printed_area headline_area offer_area "
                 "two_unit accessory accessory_area cellar_area outdoor_accessory extent extent_package "
                 "extent_variant part_whole part_addition disposition unit_count floor total_floors "
                 "prose_floor subject_floor storey_word offered_storey plot_area plot_prose "
                 "plot_prose_exact plot_attribute neighbour_plot product_class offered_use "
                 "commercial_subtype rental_colive"),
        ("PRICE", "price charge accessory_price priced_row"),
        ("LOC", "obec obec_prose body_obec street street_prose orientation stored_house_number "
                "printed_house_number parcel"),
        ("TXT", "unit_designator printed_designator unit_code labelled_unit english_unit_code "
                "slug_unit space_number plan_space position_designator named_villa lot_label "
                "agency_code agency_code_plus agency_code_colive body_align"),
        ("IMG", "interior floorplan")):
    for _n in _names.split():
        READER_FAMILY[_n] = _fam
FAMILY_ORDER: tuple[str, ...] = ("ATTR", "PRICE", "LOC", "TXT", "IMG")


def reader_matrix(fired: list[tuple[str, ...]], names: list[str], mode: str) -> np.ndarray:
    """`r64`: one 0/1 column per reader; `r5`: firings counted per C6 family; `none`: no columns."""
    if mode == "none":
        return np.zeros((len(fired), 0))
    if mode == "r64":
        col = {n: j for j, n in enumerate(names)}
        R = np.zeros((len(fired), len(names)))
        for i, f in enumerate(fired):
            for n in f:
                R[i, col[n]] = 1.0
        return R
    col = {n: j for j, n in enumerate(FAMILY_ORDER)}
    R = np.zeros((len(fired), len(FAMILY_ORDER)))
    for i, f in enumerate(fired):
        for n in f:
            R[i, col[READER_FAMILY[n]]] += 1.0
    return R


def load_cohort(name: str) -> Cohort:
    with open(OUT / f"cache/prep_{name}.pkl", "rb") as handle:
        prep = pickle.load(handle)
    with open(OUT / f"cache/ladder_{name}.pkl", "rb") as handle:
        lad = pickle.load(handle)
    keys = list(prep["keys"]) + list(prep["extra"])
    V = np.vstack([prep["V"], prep["Vx"]]) if len(prep["extra"]) else prep["V"]
    P = np.vstack([prep["P"], prep["Px"]]) if len(prep["extra"]) else prep["P"]
    c = Cohort(name, keys, len(prep["keys"]), V, P, prep["recs"], prep["labels"], lad)
    rp = OUT / f"cache/readers_{name}.pkl"
    if rp.is_file():
        with open(rp, "rb") as handle:
            r = pickle.load(handle)
        c.fired, c.reader_names = r["fired"], r["names"]
    return c


def design(c: Cohort, mode: str) -> tuple[np.ndarray, np.ndarray]:
    """The model's input: the 60 features (+ the reader columns, always present, for r64 / r5)."""
    if mode == "none":
        return c.V, c.P
    R = reader_matrix(c.fired, c.reader_names, mode)
    return np.hstack([c.V, R]), np.hstack([c.P, np.ones(R.shape, dtype=bool)])


# --- w6_gold, vectorised (model.py:430-491 exactly) -------------------------------------------

class W6:
    def __init__(self) -> None:
        raw = json.loads(MODEL.read_text())
        self.raw = raw
        self.order = raw["feature_order"]
        self.w = raw["weights"]
        self.pw = raw["presence_weights"]
        self.b = raw["intercept"]
        self.inter = raw.get("interactions") or []
        self.means = raw.get("means") or {}
        self.scales = raw.get("scales") or {}
        self.knots = raw.get("calibration") or []

    def raw_p(self, V: np.ndarray, P: np.ndarray) -> np.ndarray:
        idx = {n: i for i, n in enumerate(FEATURE_ORDER)}
        total = np.full(len(V), float(self.b))
        Z = {}
        for name in self.order:
            j = idx[name]
            val = np.where(P[:, j], V[:, j], 0.0)
            s = self.scales.get(name, 1.0) or 1.0
            z = (val - self.means.get(name, 0.0)) / s
            Z[name] = z
            total += self.w.get(name, 0.0) * z + self.pw.get(name, 0.0) * P[:, j]
        for left, right, c in self.inter:
            total += float(c) * Z[left] * Z[right]
        return 1.0 / (1.0 + np.exp(-total))

    def score(self, V: np.ndarray, P: np.ndarray) -> np.ndarray:
        p = self.raw_p(V, P)
        xs = np.array([k[0] for k in self.knots])
        ys = np.array([k[1] for k in self.knots])
        return np.interp(p, xs, ys) * (1.0 - CALIBRATION_TIE_BREAK) + CALIBRATION_TIE_BREAK * p


# --- designs ------------------------------------------------------------------------------------

def load_g1(path: str | None, keys: list[tuple[int, int]]) -> np.ndarray:
    """The G1 side file (one JSON line per pair: lo, hi and the four G1_SLOTS, written by the GPU
    image job for candidate pairs) as a column block; a pair or a slot it lacks stays missing."""
    G = np.full((len(keys), len(G1_SLOTS)), np.nan)
    if not path:
        return G
    pos = {k: i for i, k in enumerate(keys)}
    with open(path) as handle:
        for line in handle:
            r = json.loads(line)
            i = pos.get((min(r["lo"], r["hi"]), max(r["lo"], r["hi"])))
            if i is None:
                continue
            for j, slot in enumerate(G1_SLOTS):
                if r.get(slot) is not None:
                    G[i, j] = float(r[slot])
    return G


def tree_design(V: np.ndarray, P: np.ndarray, G1: np.ndarray | None = None) -> np.ndarray:
    X = np.where(P, V, np.nan)
    return np.hstack([X, G1 if G1 is not None else np.full((len(V), len(G1_SLOTS)), np.nan)])


def dense_design(V: np.ndarray, P: np.ndarray) -> np.ndarray:
    return np.hstack([np.where(P, V, 0.0), P.astype(np.float64)])


def balanced(y: np.ndarray, w: np.ndarray) -> np.ndarray:
    pos, neg = w[y == 1].sum(), w[y == 0].sum()
    out = w.copy()
    out[y == 1] *= 0.5 * (pos + neg) / max(pos, 1e-9)
    out[y == 0] *= 0.5 * (pos + neg) / max(neg, 1e-9)
    return out


class Model:
    kind: str

    def __init__(self, kind: str, seed: int = 20260927):
        self.kind, self.seed = kind, seed
        self.scaler = None
        self.clf = None
        self.iso: IsotonicRegression | None = None

    def fit(self, V, P, y, w) -> "Model":
        sw = balanced(y, w)
        if self.kind == "hgb":
            X = tree_design(V, P)
            self.cols, self.fill = [], {}
            for j in range(X.shape[1]):
                seen = np.unique(X[~np.isnan(X[:, j]), j])
                if len(seen) == 0:
                    continue  # a G1 slot with no data yet: carried in the schema, not fitted
                self.cols.append(j)
                if len(seen) == 1:
                    self.fill[j] = float(seen[0]) - 1.0  # constant-when-present: presence is the signal
            self.clf = HistGradientBoostingClassifier(
                max_iter=400, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=20,
                l2_regularization=1.0, random_state=self.seed)
            self.clf.fit(self._tree(X), y, sample_weight=sw)
        elif self.kind == "mlp":
            X = dense_design(V, P)
            self.scaler = StandardScaler().fit(X)
            self.clf = MLPClassifier(hidden_layer_sizes=(64, 32), alpha=1e-3, max_iter=400,
                                     early_stopping=False, random_state=self.seed)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self.clf.fit(self.scaler.transform(X), y, sample_weight=sw)
        elif self.kind == "lr":
            X = dense_design(V, P)
            self.scaler = StandardScaler().fit(X)
            self.clf = LogisticRegression(C=0.5, max_iter=2000)
            self.clf.fit(self.scaler.transform(X), y, sample_weight=sw)
        else:
            raise ValueError(self.kind)
        return self

    def _tree(self, X: np.ndarray) -> np.ndarray:
        X = X.copy()
        for j, value in self.fill.items():
            X[np.isnan(X[:, j]), j] = value
        return X[:, self.cols]

    def raw(self, V, P) -> np.ndarray:
        if self.kind == "hgb":
            return self.clf.predict_proba(self._tree(tree_design(V, P)))[:, 1]
        return self.clf.predict_proba(self.scaler.transform(dense_design(V, P)))[:, 1]


def fit_isotonic(raw: np.ndarray, y: np.ndarray, w: np.ndarray) -> IsotonicRegression:
    return IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(raw, y, sample_weight=w)


def auc(scores: np.ndarray, y: np.ndarray) -> float | None:
    pos, neg = scores[y == 1], scores[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    order = np.argsort(np.concatenate([neg, pos]), kind="mergesort")
    ranks = np.empty(len(order))
    allv = np.concatenate([neg, pos])[order]
    # average ranks for ties
    i = 0
    r = np.arange(1, len(allv) + 1, dtype=float)
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1] == allv[i]:
            j += 1
        r[i:j + 1] = (i + j + 2) / 2.0
        i = j + 1
    ranks[order] = r
    rp = ranks[len(neg):].sum()
    return float((rp - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg)))
