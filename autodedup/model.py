"""The calibrated stdlib scorer (PROGRAM.md E21): logistic regression, no numpy, no sklearn.

Inference is a dot product over `(value, present)` pairs — E12's two terms per feature, so
absence learns its own weight — plus a handful of hand-specified interaction terms for the
domain physics a linear model cannot see (§6). Features are standardised with means/scales
stored on the artifact, so a JSON round-trip reproduces a score exactly, and `hand_initialised`
carries the priors that let W2 score before a single label exists.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from autodedup.features import FEATURE_ORDER

Feats = Mapping[str, tuple[float, bool]]

MODEL_KIND: str = "autodedup.logistic"
MODEL_FORMAT: int = 1

# The calibration map is piecewise LINEAR between bin anchors, not a step per bin: E33 processes
# cluster edges in descending confidence and E25 moves T_lo to buy band width, and both need an
# ordering finer than `bins` distinct values. The tie-break keeps it STRICTLY increasing even
# across a flat isotonic segment, so ranking inside an anchor interval survives calibration.
CALIBRATION_TIE_BREAK: float = 1e-6

# `fit` iterates to a gradient tolerance; the epoch count is the ceiling, not the plan.
FIT_TOLERANCE: float = 1e-6


def sigmoid(z: float) -> float:
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    exp_z = math.exp(z)
    return exp_z / (1.0 + exp_z)


def auc(scores: Sequence[float], labels: Sequence[int]) -> float:
    """Rank-based ROC AUC with tie averaging; 0.5 when one class is absent."""
    if len(scores) != len(labels):
        raise ValueError(f"length mismatch: {len(scores)} scores vs {len(labels)} labels")
    positives = sum(1 for label in labels if label > 0)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        return 0.5
    order = sorted(range(len(scores)), key=lambda index: scores[index])
    ranks = [0.0] * len(scores)
    index = 0
    while index < len(order):
        stop = index
        while stop + 1 < len(order) and scores[order[stop + 1]] == scores[order[index]]:
            stop += 1
        shared = (index + stop) / 2.0 + 1.0
        for position in range(index, stop + 1):
            ranks[order[position]] = shared
        index = stop + 1
    rank_sum = sum(ranks[i] for i, label in enumerate(labels) if label > 0)
    return (rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def expected_calibration_error(
    probs: Sequence[float], labels: Sequence[int], bins: int = 10
) -> float:
    total = len(probs)
    if total == 0:
        return 0.0
    sums = [0.0] * bins
    hits = [0.0] * bins
    counts = [0] * bins
    for prob, label in zip(probs, labels):
        slot = min(bins - 1, max(0, int(prob * bins)))
        sums[slot] += prob
        hits[slot] += 1.0 if label > 0 else 0.0
        counts[slot] += 1
    error = 0.0
    for slot in range(bins):
        if counts[slot] == 0:
            continue
        mean_prob = sums[slot] / counts[slot]
        rate = hits[slot] / counts[slot]
        error += (counts[slot] / total) * abs(rate - mean_prob)
    return error


def _pool_adjacent_violators(
    rates: list[float], weights: list[float]
) -> list[float]:
    """Isotonic regression over bin rates (PAVA), weighted by bin population."""
    values = list(rates)
    masses = list(weights)
    sizes = [1] * len(values)
    index = 0
    while index < len(values) - 1:
        if values[index] <= values[index + 1]:
            index += 1
            continue
        mass = masses[index] + masses[index + 1]
        merged = (
            (values[index] * masses[index] + values[index + 1] * masses[index + 1]) / mass
            if mass > 0.0
            else (values[index] + values[index + 1]) / 2.0
        )
        values[index] = merged
        masses[index] = mass
        sizes[index] += sizes[index + 1]
        del values[index + 1]
        del masses[index + 1]
        del sizes[index + 1]
        if index > 0:
            index -= 1
    out: list[float] = []
    for value, size in zip(values, sizes):
        out.extend([value] * size)
    return out


@dataclass(slots=True)
class LogisticModel:
    feature_order: tuple[str, ...]
    weights: dict[str, float]
    presence_weights: dict[str, float]
    intercept: float
    interactions: list[tuple[str, str, float]] = field(default_factory=list)
    calibration: list[tuple[float, float]] | None = None
    means: dict[str, float] = field(default_factory=dict)
    scales: dict[str, float] = field(default_factory=dict)
    version: str = "hand_v1"
    fit_report: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.feature_order = tuple(self.feature_order)
        self.interactions = [(a, b, float(w)) for a, b, w in self.interactions]

    def standardise(self, name: str, value: float) -> float:
        scale = self.scales.get(name, 1.0)
        return (value - self.means.get(name, 0.0)) / (scale if scale else 1.0)

    def score(self, feats: Feats) -> float:
        """The linear score (log-odds) before the sigmoid — the UI's per-feature audit basis."""
        standardised: dict[str, float] = {}
        total = self.intercept
        for name in self.feature_order:
            value, present = feats.get(name, (0.0, False))
            raw = float(value) if present else 0.0
            term = self.standardise(name, raw)
            standardised[name] = term
            weight = self.weights.get(name)
            if weight:
                total += weight * term
            presence_weight = self.presence_weights.get(name)
            if presence_weight:
                total += presence_weight * (1.0 if present else 0.0)
        for left, right, coefficient in self.interactions:
            if coefficient:
                total += coefficient * standardised.get(left, 0.0) * standardised.get(right, 0.0)
        return total

    def contributions(self, feats: Feats) -> dict[str, float]:
        """Log-odds contributions, value and presence terms apart (E21: the model must be legible)."""
        out: dict[str, float] = {}
        standardised: dict[str, float] = {}
        for name in self.feature_order:
            value, present = feats.get(name, (0.0, False))
            raw = float(value) if present else 0.0
            term = self.standardise(name, raw)
            standardised[name] = term
            out[name] = self.weights.get(name, 0.0) * term
            presence_term = self.presence_weights.get(name, 0.0) * (1.0 if present else 0.0)
            if presence_term:
                out[f"{name}:present"] = presence_term
        for left, right, coefficient in self.interactions:
            out[f"{left}*{right}"] = (
                coefficient * standardised.get(left, 0.0) * standardised.get(right, 0.0)
            )
        return out

    def predict_proba(self, feats: Feats) -> float:
        return self.apply_calibration(sigmoid(self.score(feats)))

    def apply_calibration(self, probability: float) -> float:
        """Monotone piecewise-linear interpolation over the stored `(anchor, rate)` knots."""
        knots = self.calibration
        if not knots:
            return probability
        if len(knots) == 1 or probability <= knots[0][0]:
            value = knots[0][1]
        elif probability >= knots[-1][0]:
            value = knots[-1][1]
        else:
            value = knots[-1][1]
            for index in range(1, len(knots)):
                right_x, right_y = knots[index]
                if probability <= right_x:
                    left_x, left_y = knots[index - 1]
                    span = right_x - left_x
                    share = (probability - left_x) / span if span > 0.0 else 1.0
                    value = left_y + share * (right_y - left_y)
                    break
        return value * (1.0 - CALIBRATION_TIE_BREAK) + CALIBRATION_TIE_BREAK * probability

    def fit(
        self,
        rows: Sequence[Feats],
        labels: Sequence[int],
        *,
        l2: float = 1e-3,
        epochs: int = 3000,
        lr: float = 0.2,
        tol: float = FIT_TOLERANCE,
        sample_weights: Sequence[float] | None = None,
    ) -> None:
        """Full-batch gradient descent on standardised features; L2 on weights, not intercept.

        Runs to a mean-|gradient| tolerance with `epochs` as the ceiling, and leaves the
        convergence evidence on `fit_report` — an under-fit model shrinks every weight toward the
        intercept, which moves the calibrated probability T_hi/T_lo are read off (E21/E22)."""
        if len(rows) != len(labels):
            raise ValueError(f"length mismatch: {len(rows)} rows vs {len(labels)} labels")
        if not rows:
            raise ValueError("fit needs at least one row")
        names = self.feature_order
        raw: list[list[float]] = []
        presence: list[list[float]] = []
        for row in rows:
            values: list[float] = []
            flags: list[float] = []
            for name in names:
                value, present = row.get(name, (0.0, False))
                values.append(float(value) if present else 0.0)
                flags.append(1.0 if present else 0.0)
            raw.append(values)
            presence.append(flags)
        count = len(raw)
        self.means = {}
        self.scales = {}
        for index, name in enumerate(names):
            column = [row[index] for row in raw]
            mean = sum(column) / count
            variance = sum((value - mean) ** 2 for value in column) / count
            deviation = math.sqrt(variance)
            self.means[name] = mean
            self.scales[name] = deviation if deviation > 1e-9 else 1.0
        offsets = [self.means[name] for name in names]
        divisors = [self.scales[name] for name in names]
        design = [
            [(value - offsets[i]) / divisors[i] for i, value in enumerate(row)] for row in raw
        ]
        position = {name: index for index, name in enumerate(names)}
        unknown = sorted(
            {name for left, right, _ in self.interactions for name in (left, right)}
            - set(position)
        )
        if unknown:
            raise ValueError(f"interaction names features outside feature_order: {unknown}")
        pairs = [(position[left], position[right]) for left, right, _ in self.interactions]
        products = [[row[a] * row[b] for a, b in pairs] for row in design]
        weights = [self.weights.get(name, 0.0) for name in names]
        presence_weights = [self.presence_weights.get(name, 0.0) for name in names]
        interaction_weights = [coefficient for _, _, coefficient in self.interactions]
        intercept = self.intercept
        masses = (
            [float(weight) for weight in sample_weights]
            if sample_weights is not None
            else [1.0] * count
        )
        if len(masses) != count:
            raise ValueError("sample_weights length mismatch")
        total_mass = sum(masses) or 1.0
        n_features = len(names)
        epochs_run = 0
        mean_abs_grad = 0.0
        for _ in range(epochs):
            epochs_run += 1
            grad_w = [0.0] * n_features
            grad_p = [0.0] * n_features
            grad_i = [0.0] * len(pairs)
            grad_b = 0.0
            for index in range(count):
                row = design[index]
                flags = presence[index]
                product = products[index]
                z = intercept + math.sumprod(weights, row) + math.sumprod(presence_weights, flags)
                if pairs:
                    z += math.sumprod(interaction_weights, product)
                error = (sigmoid(z) - (1.0 if labels[index] > 0 else 0.0)) * masses[index]
                if error == 0.0:
                    continue
                grad_b += error
                for feature in range(n_features):
                    grad_w[feature] += error * row[feature]
                    grad_p[feature] += error * flags[feature]
                for slot in range(len(pairs)):
                    grad_i[slot] += error * product[slot]
            intercept -= lr * (grad_b / total_mass)
            for feature in range(n_features):
                weights[feature] -= lr * (grad_w[feature] / total_mass + l2 * weights[feature])
                presence_weights[feature] -= lr * (
                    grad_p[feature] / total_mass + l2 * presence_weights[feature]
                )
            for slot in range(len(pairs)):
                interaction_weights[slot] -= lr * (
                    grad_i[slot] / total_mass + l2 * interaction_weights[slot]
                )
            magnitude = abs(grad_b) + sum(map(abs, grad_w)) + sum(map(abs, grad_p))
            magnitude += sum(map(abs, grad_i))
            mean_abs_grad = magnitude / (total_mass * (2 * n_features + len(pairs) + 1))
            if mean_abs_grad <= tol:
                break
        log_loss = 0.0
        for index in range(count):
            row = design[index]
            z = intercept + math.sumprod(weights, row)
            z += math.sumprod(presence_weights, presence[index])
            if pairs:
                z += math.sumprod(interaction_weights, products[index])
            probability = min(1.0 - 1e-12, max(1e-12, sigmoid(z)))
            target = 1.0 if labels[index] > 0 else 0.0
            log_loss -= masses[index] * (
                target * math.log(probability) + (1.0 - target) * math.log(1.0 - probability)
            )
        self.fit_report = {
            "epochs_run": float(epochs_run),
            "mean_abs_grad": mean_abs_grad,
            "log_loss": log_loss / total_mass,
            "converged": 1.0 if mean_abs_grad <= tol else 0.0,
        }
        self.intercept = intercept
        self.weights = {name: weights[index] for index, name in enumerate(names)}
        self.presence_weights = {
            name: presence_weights[index] for index, name in enumerate(names)
        }
        self.interactions = [
            (left, right, interaction_weights[slot])
            for slot, (left, right, _) in enumerate(self.interactions)
        ]
        self.calibration = None

    def calibrate(
        self, probs: Sequence[float], labels: Sequence[int], bins: int = 10
    ) -> float:
        """Store the isotonic-over-bins KNOTS and return the PRE-calibration 10-bin ECE (E21).

        A knot is `(mean predicted probability in the bin, isotonic rate)`; `apply_calibration`
        interpolates between knots, so the map is monotone without being a step function."""
        if len(probs) != len(labels):
            raise ValueError(f"length mismatch: {len(probs)} probs vs {len(labels)} labels")
        error = expected_calibration_error(probs, labels, bins)
        counts = [0] * bins
        hits = [0.0] * bins
        sums = [0.0] * bins
        for prob, label in zip(probs, labels):
            slot = min(bins - 1, max(0, int(prob * bins)))
            counts[slot] += 1
            sums[slot] += prob
            hits[slot] += 1.0 if label > 0 else 0.0
        anchors: list[float] = []
        rates: list[float] = []
        masses: list[float] = []
        for slot in range(bins):
            if counts[slot] == 0:
                continue
            anchor = sums[slot] / counts[slot]
            if anchors and anchor <= anchors[-1]:
                anchor = anchors[-1] + 1e-9
            anchors.append(anchor)
            rates.append(hits[slot] / counts[slot])
            masses.append(float(counts[slot]))
        if not anchors:
            self.calibration = None
            return error
        smoothed = _pool_adjacent_violators(rates, masses)
        self.calibration = [(anchor, value) for anchor, value in zip(anchors, smoothed)]
        return error

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": MODEL_KIND,
            "format": MODEL_FORMAT,
            "version": self.version,
            "feature_order": list(self.feature_order),
            "weights": dict(self.weights),
            "presence_weights": dict(self.presence_weights),
            "intercept": self.intercept,
            "interactions": [[left, right, weight] for left, right, weight in self.interactions],
            "calibration": (
                [[edge, value] for edge, value in self.calibration] if self.calibration else None
            ),
            "means": dict(self.means),
            "scales": dict(self.scales),
            "fit_report": dict(self.fit_report),
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any] | str) -> "LogisticModel":
        data = json.loads(payload) if isinstance(payload, str) else payload
        calibration = data.get("calibration")
        return cls(
            feature_order=tuple(data["feature_order"]),
            weights={str(k): float(v) for k, v in (data.get("weights") or {}).items()},
            presence_weights={
                str(k): float(v) for k, v in (data.get("presence_weights") or {}).items()
            },
            intercept=float(data.get("intercept", 0.0)),
            interactions=[
                (str(item[0]), str(item[1]), float(item[2]))
                for item in (data.get("interactions") or [])
            ],
            calibration=(
                [(float(item[0]), float(item[1])) for item in calibration] if calibration else None
            ),
            means={str(k): float(v) for k, v in (data.get("means") or {}).items()},
            scales={str(k): float(v) for k, v in (data.get("scales") or {}).items()},
            version=str(data.get("version", "hand_v1")),
            fit_report={str(k): float(v) for k, v in (data.get("fit_report") or {}).items()},
        )

    @staticmethod
    def hand_initialised() -> "LogisticModel":
        return hand_initialised()


# Priors from §6, on RAW feature units (means 0 / scales 1), so every weight below is readable
# as "log-odds per unit of this feature" before any label exists.
PRIOR_WEIGHTS: dict[str, float] = {
    "area_rel_diff": -6.0,
    "area_exact": 1.2,
    "dispo_equal": 0.9,
    "floor_diff": -1.2,
    "total_floors_equal": 0.2,
    "attr_agreements": 0.10,
    "attr_contradictions": -0.9,
    "attr_agreements_rare": 0.08,
    "price_last_ratio": 0.5,
    "ppm2_rel_diff": -1.5,
    "price_path_event_match": 1.5,
    "jaccard_shingle": 0.8,
    "containment_max": 1.2,
    "tfidf_cos": 0.9,
    "simhash_hamming": -0.03,
    "len_ratio": 0.2,
    "rare_token_overlap": 0.35,
    "numeric_fact_overlap": 0.15,
    "numeral_conflict": -1.5,
    "same_broker_key": 0.4,
    "same_broker_identity": 0.5,
    "broker_known_both": 0.0,
    "same_ruian_adm_kod": 2.2,
    "same_street_key": 0.6,
    "same_house_number": 0.9,
    "same_psc": 0.3,
    "dist_norm": -0.8,
    "same_exact_pin": 0.5,
    "pin_pop": -0.25,
    "phash_tight_matches": 0.15,
    "phash_loose_matches": 0.03,
    "phash_match_ratio": 2.0,
    "clip_max_cos": 0.6,
    "clip_mean_top3_cos": 0.4,
    "seq_monotone_ratio": 0.5,
    "interior_match_ratio": 1.6,
    "exterior_match_ratio": 0.15,
    "plan_match_ratio": -0.3,
    "catalog_ratio_max": -1.2,
    "n_images_min": 0.02,
    "gap_days": 0.0,
    # E7: a cross-portal advert live on two portals at once is the PRIMARY positive class, so
    # overlap carries no global sign. The design's only overlap penalty is K-B's same-source,
    # same-broker clause (E24), and that is where the interaction below puts it.
    "overlap_days": 0.0,
    "both_active": 0.0,
    "same_source": 0.0,
}

PRIOR_PRESENCE_WEIGHTS: dict[str, float] = {
    "area_rel_diff": 0.3,
    "dispo_equal": 0.1,
    "same_ruian_adm_kod": 0.2,
    "price_path_event_match": 0.2,
    "phash_match_ratio": 0.1,
    "dist_norm": 0.1,
    "rare_token_overlap": 0.1,
}

PRIOR_INTERACTIONS: list[tuple[str, str, float]] = [
    ("interior_match_ratio", "catalog_ratio_max", -1.0),
    ("catalog_ratio_max", "phash_match_ratio", -1.5),
    ("rare_token_overlap", "same_source", 0.15),
    ("dist_norm", "pin_pop", -0.20),
    ("area_exact", "dispo_equal", 0.8),
    ("same_broker_key", "containment_max", 0.8),
    ("price_path_event_match", "same_broker_key", 0.3),
    ("overlap_days", "same_source", -0.002),
]

PRIOR_INTERCEPT: float = -4.0


def hand_initialised() -> LogisticModel:
    """The pre-label scorer of §6: priors on raw units, identity standardisation, uncalibrated."""
    return LogisticModel(
        feature_order=FEATURE_ORDER,
        weights={name: PRIOR_WEIGHTS.get(name, 0.0) for name in FEATURE_ORDER},
        presence_weights={
            name: PRIOR_PRESENCE_WEIGHTS.get(name, 0.0) for name in FEATURE_ORDER
        },
        intercept=PRIOR_INTERCEPT,
        interactions=[
            (left, right, weight)
            for left, right, weight in PRIOR_INTERACTIONS
            if left in FEATURE_ORDER and right in FEATURE_ORDER
        ],
        calibration=None,
        means={name: 0.0 for name in FEATURE_ORDER},
        scales={name: 1.0 for name in FEATURE_ORDER},
        version="hand_v1",
    )
