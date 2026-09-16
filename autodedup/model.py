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

# `fit_irls` iterates to a step-size tolerance instead: a Newton step that moves no coefficient by
# more than this IS the penalised MLE, to the precision the solve carries.
IRLS_TOLERANCE: float = 1e-8

# The line search halves at most this many times before it declares the direction unusable. It is
# a ceiling on wasted work, never a convergence criterion — an exhausted search reports the step it
# could NOT take, so a stall can never be read as a converged fit.
IRLS_MAX_HALVINGS: int = 40

# A design column whose train-fold range is under this is constant: not identified, and dropped.
CONSTANT_COLUMN_EPS: float = 1e-12


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


# IRLS solves a (d x d) symmetric system per Newton step; d is ~100 here, so a dense stdlib
# elimination is cheaper than any dependency. A degenerate column (a zero-variance feature, or a
# presence flag collinear with the intercept when l2 is 0) has no finite step: pin it at 0 rather
# than dividing by a pivot that is numerically noise.
SOLVE_PIVOT_EPS: float = 1e-12


def _solve_linear_system(
    matrix: Sequence[Sequence[float]], rhs: Sequence[float]
) -> tuple[list[float], list[int]]:
    """Gaussian elimination with partial pivoting, returning `(solution, pinned columns)`.

    Pinning a degenerate column at 0 also DISCARDS the equation sitting in its row, so what comes
    back can solve a different system than the one asked for: every such column is named in
    `pinned` and the caller decides whether the vector is still a usable direction. A non-finite
    entry raises instead of propagating a NaN into the weights."""
    size = len(rhs)
    if size == 0:
        return [], []
    for row in matrix:
        for value in row:
            if not math.isfinite(value):
                raise ValueError("non-finite value in the linear system")
    for value in rhs:
        if not math.isfinite(value):
            raise ValueError("non-finite value in the linear system")
    augmented = [list(row) + [value] for row, value in zip(matrix, rhs)]
    magnitude = max((abs(value) for row in matrix for value in row), default=0.0)
    tiny = magnitude * SOLVE_PIVOT_EPS
    pinned: list[int] = []
    for column in range(size):
        pivot_row = max(range(column, size), key=lambda index: abs(augmented[index][column]))
        if abs(augmented[pivot_row][column]) <= tiny:
            discarded = augmented[column]
            carried = max(
                (abs(discarded[index]) for index in range(column + 1, size)), default=0.0
            )
            if carried > tiny or abs(discarded[size]) > tiny:
                pinned.append(column)
            for index in range(column, size + 1):
                augmented[column][index] = 1.0 if index == column else 0.0
            continue
        if pivot_row != column:
            augmented[column], augmented[pivot_row] = augmented[pivot_row], augmented[column]
        pivot = augmented[column][column]
        for index in range(column + 1, size):
            factor = augmented[index][column] / pivot
            if factor == 0.0:
                continue
            row = augmented[index]
            head = augmented[column]
            for position in range(column, size + 1):
                row[position] -= factor * head[position]
    out = [0.0] * size
    for column in reversed(range(size)):
        row = augmented[column]
        total = row[size] - sum(row[index] * out[index] for index in range(column + 1, size))
        out[column] = total / row[column]
    return out, pinned


@dataclass(slots=True)
class _Design:
    """The parameterisation both fits share: centred/scaled values, presence flags, products."""

    design: list[list[float]]
    presence: list[list[float]]
    products: list[list[float]]
    masses: list[float]
    total_mass: float
    means: dict[str, float]
    scales: dict[str, float]


def _standardised_design(
    rows: Sequence[Feats],
    names: Sequence[str],
    interactions: Sequence[tuple[str, str, float]],
    sample_weights: Sequence[float] | None,
) -> _Design:
    """Build the design ONCE for both optimisers — a JSON round trip reproduces a score exactly
    only while `fit` and `fit_irls` standardise identically."""
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
    means: dict[str, float] = {}
    scales: dict[str, float] = {}
    for index, name in enumerate(names):
        column = [row[index] for row in raw]
        mean = sum(column) / count
        variance = sum((value - mean) ** 2 for value in column) / count
        deviation = math.sqrt(variance)
        means[name] = mean
        scales[name] = deviation if deviation > 1e-9 else 1.0
    offsets = [means[name] for name in names]
    divisors = [scales[name] for name in names]
    design = [[(value - offsets[i]) / divisors[i] for i, value in enumerate(row)] for row in raw]
    position = {name: index for index, name in enumerate(names)}
    unknown = sorted(
        {name for left, right, _ in interactions for name in (left, right)} - set(position)
    )
    if unknown:
        raise ValueError(f"interaction names features outside feature_order: {unknown}")
    pairs = [(position[left], position[right]) for left, right, _ in interactions]
    products = [[row[a] * row[b] for a, b in pairs] for row in design]
    masses = (
        [float(weight) for weight in sample_weights]
        if sample_weights is not None
        else [1.0] * count
    )
    if len(masses) != count:
        raise ValueError("sample_weights length mismatch")
    return _Design(design, presence, products, masses, sum(masses) or 1.0, means, scales)


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
        built = _standardised_design(rows, names, self.interactions, sample_weights)
        design, presence, products = built.design, built.presence, built.products
        masses, total_mass = built.masses, built.total_mass
        self.means, self.scales = built.means, built.scales
        count = len(design)
        weights = [self.weights.get(name, 0.0) for name in names]
        presence_weights = [self.presence_weights.get(name, 0.0) for name in names]
        interaction_weights = [coefficient for _, _, coefficient in self.interactions]
        intercept = self.intercept
        n_pairs = len(self.interactions)
        n_features = len(names)
        epochs_run = 0
        mean_abs_grad = 0.0
        for _ in range(epochs):
            epochs_run += 1
            grad_w = [0.0] * n_features
            grad_p = [0.0] * n_features
            grad_i = [0.0] * n_pairs
            grad_b = 0.0
            for index in range(count):
                row = design[index]
                flags = presence[index]
                product = products[index]
                z = intercept + math.sumprod(weights, row) + math.sumprod(presence_weights, flags)
                if n_pairs:
                    z += math.sumprod(interaction_weights, product)
                error = (sigmoid(z) - (1.0 if labels[index] > 0 else 0.0)) * masses[index]
                if error == 0.0:
                    continue
                grad_b += error
                for feature in range(n_features):
                    grad_w[feature] += error * row[feature]
                    grad_p[feature] += error * flags[feature]
                for slot in range(n_pairs):
                    grad_i[slot] += error * product[slot]
            intercept -= lr * (grad_b / total_mass)
            for feature in range(n_features):
                weights[feature] -= lr * (grad_w[feature] / total_mass + l2 * weights[feature])
                presence_weights[feature] -= lr * (
                    grad_p[feature] / total_mass + l2 * presence_weights[feature]
                )
            for slot in range(n_pairs):
                interaction_weights[slot] -= lr * (
                    grad_i[slot] / total_mass + l2 * interaction_weights[slot]
                )
            magnitude = abs(grad_b) + sum(map(abs, grad_w)) + sum(map(abs, grad_p))
            magnitude += sum(map(abs, grad_i))
            mean_abs_grad = magnitude / (total_mass * (2 * n_features + n_pairs + 1))
            if mean_abs_grad <= tol:
                break
        log_loss = 0.0
        for index in range(count):
            row = design[index]
            z = intercept + math.sumprod(weights, row)
            z += math.sumprod(presence_weights, presence[index])
            if n_pairs:
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

    def fit_irls(
        self,
        rows: Sequence[Feats],
        labels: Sequence[int],
        *,
        l2: float = 1e-3,
        max_iter: int = 50,
        tol: float = IRLS_TOLERANCE,
        sample_weights: Sequence[float] | None = None,
    ) -> dict[str, Any]:
        """Newton / IRLS on the same standardised parameterisation `fit` uses, L2 off the intercept.

        Gradient descent on ~100 correlated columns needs thousands of epochs and still stops short
        of the optimum, which shrinks every weight toward the intercept and silently moves the
        calibrated probability E21 reads T_hi/T_lo off. Each Newton step solves
        `(X'WX + l2 R) d = -g` exactly, so the fit lands on the penalised MLE in a handful of
        iterations; the step is halved until the penalised objective actually falls, which keeps a
        near-separable stratum from overshooting.

        Returns the DESIGN report — the unidentified terms dropped from the fit and the
        exactly-collinear groups left in it — because at the MLE those are what a per-feature
        audit would otherwise read as independent evidence."""
        if len(rows) != len(labels):
            raise ValueError(f"length mismatch: {len(rows)} rows vs {len(labels)} labels")
        if not rows:
            raise ValueError("fit needs at least one row")
        if max_iter < 1:
            # Zero iterations would report an infinite `max_abs_delta` — not a number a JSON
            # artifact can carry, and not a fit either.
            raise ValueError(f"max_iter must be at least 1, got {max_iter}")
        names = self.feature_order
        built = _standardised_design(rows, names, self.interactions, sample_weights)
        masses, total_mass = built.masses, built.total_mass
        self.means, self.scales = built.means, built.scales
        count = len(rows)
        n_features = len(names)
        n_pairs = len(self.interactions)
        targets = [1.0 if label > 0 else 0.0 for label in labels]

        # One row-major matrix for the linear predictor, one column-major copy for the Gram: the
        # intercept is column 0, then the value terms, the presence terms, the interactions.
        matrix = [
            [1.0] + built.design[index] + built.presence[index] + built.products[index]
            for index in range(count)
        ]
        width = 1 + 2 * n_features + n_pairs
        term_names = (
            ["intercept"]
            + list(names)
            + [f"{name}:present" for name in names]
            + [f"{left}*{right}" for left, right, _ in self.interactions]
        )
        columns = [[matrix[index][slot] for index in range(count)] for slot in range(width)]
        # A column that never varies over the train fold is not identified: a presence flag that is
        # always 1 IS the intercept again. Left in, ridge splits one effect across the duplicate
        # coordinates and every share reads as its own evidence; dropped, the weight stays 0 and
        # the term is named, so ~100 nominal parameters are reported as the fewer real ones.
        active = [0] + [
            slot for slot in range(1, width)
            if max(columns[slot]) - min(columns[slot]) > CONSTANT_COLUMN_EPS
        ]
        carried = set(active)
        dropped = [term_names[slot] for slot in range(1, width) if slot not in carried]
        # Exact collinearity is NOT dropped (which of the copies is the real signal is a modelling
        # call, not an arithmetic one) — it is reported, because ridge divides the effect evenly
        # among the copies and the audit must not add those shares up.
        by_column: dict[tuple[float, ...], list[str]] = {}
        for slot in active:
            by_column.setdefault(tuple(columns[slot]), []).append(term_names[slot])
        duplicate_groups = [members for members in by_column.values() if len(members) > 1]

        size = len(active)
        reduced = [[row[slot] for slot in active] for row in matrix]
        reduced_columns = [columns[slot] for slot in active]
        penalties = [0.0 if slot == 0 else l2 for slot in active]
        warm = (
            [self.intercept]
            + [self.weights.get(name, 0.0) for name in names]
            + [self.presence_weights.get(name, 0.0) for name in names]
            + [coefficient for _, _, coefficient in self.interactions]
        )
        beta = [warm[slot] for slot in active]

        def objective(vector: Sequence[float]) -> float:
            total = 0.0
            for index in range(count):
                z = math.sumprod(vector, reduced[index])
                probability = min(1.0 - 1e-12, max(1e-12, sigmoid(z)))
                total -= masses[index] * (
                    targets[index] * math.log(probability)
                    + (1.0 - targets[index]) * math.log(1.0 - probability)
                )
            penalty = sum(penalties[slot] * vector[slot] * vector[slot] for slot in range(size))
            return total / total_mass + 0.5 * penalty

        current = objective(beta)
        iterations = 0
        max_abs_delta = float("inf")
        converged = False
        pinned_terms: list[str] = []
        fallbacks = 0
        for _ in range(max_iter):
            iterations += 1
            residuals: list[float] = []
            hessian_weights: list[float] = []
            for index in range(count):
                probability = sigmoid(math.sumprod(beta, reduced[index]))
                residuals.append(masses[index] * (probability - targets[index]) / total_mass)
                variance = probability * (1.0 - probability)
                hessian_weights.append(
                    masses[index] * (variance if variance > 1e-12 else 1e-12) / total_mass
                )
            gradient = [
                math.sumprod(residuals, reduced_columns[slot]) + penalties[slot] * beta[slot]
                for slot in range(size)
            ]
            hessian = [[0.0] * size for _ in range(size)]
            for slot in range(size):
                weighted = [
                    hessian_weights[index] * reduced_columns[slot][index] for index in range(count)
                ]
                for other in range(slot, size):
                    value = math.sumprod(weighted, reduced_columns[other])
                    hessian[slot][other] = value
                    hessian[other][slot] = value
                hessian[slot][slot] += penalties[slot]
            step, pinned = _solve_linear_system(hessian, [-value for value in gradient])
            for slot in pinned:
                name = term_names[active[slot]]
                if name not in pinned_terms:
                    pinned_terms.append(name)
            # A Newton step descends only while the penalised Hessian is positive definite; when a
            # rank-deficient solve pins a column the vector can point UPHILL, and halving an uphill
            # direction 40 times produces a step small enough to pass any tolerance. Test the
            # direction FIRST and fall back to steepest descent, so `converged` cannot be bought
            # by shrinking a step nobody took.
            if math.sumprod(gradient, step) >= 0.0:
                step = [-value for value in gradient]
                fallbacks += 1
            if not any(step):
                max_abs_delta = 0.0
                converged = True
                break
            scale = 1.0
            candidate = [beta[slot] + step[slot] for slot in range(size)]
            value = objective(candidate)
            halvings = 0
            # `not (value <= current)` and not `value > current`: a NaN objective must be REJECTED.
            while not (value <= current) and halvings < IRLS_MAX_HALVINGS:
                scale /= 2.0
                candidate = [beta[slot] + scale * step[slot] for slot in range(size)]
                value = objective(candidate)
                halvings += 1
            if not (value <= current):
                # The search exhausted its halvings without a decrease. Report the step it could
                # NOT take rather than the 2^-40 shard it last tried, so a stall short of the
                # optimum never lands under `tol` and reads as convergence.
                max_abs_delta = max((abs(item) for item in step), default=0.0)
                break
            beta = candidate
            current = value
            max_abs_delta = max((abs(scale * item) for item in step), default=0.0)
            if max_abs_delta <= tol:
                converged = True
                break

        log_loss = 0.0
        for index in range(count):
            probability = min(
                1.0 - 1e-12, max(1e-12, sigmoid(math.sumprod(beta, reduced[index])))
            )
            log_loss -= masses[index] * (
                targets[index] * math.log(probability)
                + (1.0 - targets[index]) * math.log(1.0 - probability)
            )
        self.fit_report = {
            "iterations": float(iterations),
            "max_abs_delta": max_abs_delta,
            "log_loss": log_loss / total_mass,
            "converged": 1.0 if converged else 0.0,
            "terms": float(width),
            "terms_identified": float(size),
            "terms_dropped": float(len(dropped)),
            "duplicate_terms": float(sum(len(group) - 1 for group in duplicate_groups)),
            "pinned_terms": float(len(pinned_terms)),
            "gradient_fallbacks": float(fallbacks),
        }
        full = [0.0] * width
        for position, slot in enumerate(active):
            full[slot] = beta[position]
        self.intercept = full[0]
        self.weights = {name: full[1 + index] for index, name in enumerate(names)}
        self.presence_weights = {
            name: full[1 + n_features + index] for index, name in enumerate(names)
        }
        self.interactions = [
            (left, right, full[1 + 2 * n_features + slot])
            for slot, (left, right, _) in enumerate(self.interactions)
        ]
        self.calibration = None
        return {
            "terms": width,
            "terms_identified": size,
            "dropped_terms": dropped,
            "duplicate_groups": duplicate_groups,
            "pinned_terms": pinned_terms,
            "gradient_fallbacks": fallbacks,
        }

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
