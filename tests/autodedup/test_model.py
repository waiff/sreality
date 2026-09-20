"""The stdlib scorer: priors, standardised fit, calibration, and the JSON artifact round-trip."""

from __future__ import annotations

import json
import math
import random
import warnings
from pathlib import Path
from typing import Any

import pytest

from autodedup import features as ft
from autodedup import model as model_module
from autodedup.model import (
    LogisticModel,
    auc,
    expected_calibration_error,
    hand_initialised,
    sigmoid,
)


ROOT = Path(__file__).resolve().parents[2]


def feats(**kwargs: float | tuple[float, bool]) -> dict[str, tuple[float, bool]]:
    row: dict[str, tuple[float, bool]] = {}
    for name, value in kwargs.items():
        row[name] = value if isinstance(value, tuple) else (float(value), True)
    return row


def test_auc_helper_handles_separation_inversion_and_ties() -> None:
    assert auc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == 1.0
    assert auc([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]) == 0.0
    assert auc([0.5, 0.5, 0.5, 0.5], [0, 1, 0, 1]) == 0.5
    assert auc([0.1, 0.2], [0, 0]) == 0.5  # one class absent
    with pytest.raises(ValueError):
        auc([0.1], [0, 1])


def test_hand_initialised_covers_the_feature_vocabulary_and_orders_evidence_sanely() -> None:
    model = hand_initialised()
    assert model.feature_order == ft.FEATURE_ORDER
    assert set(model.weights) == set(ft.FEATURE_ORDER)
    assert set(model.presence_weights) == set(ft.FEATURE_ORDER)
    for left, right, _ in model.interactions:
        assert left in ft.FEATURE_ORDER and right in ft.FEATURE_ORDER

    nothing = model.predict_proba({})
    strong = model.predict_proba(
        feats(
            same_ruian_adm_kod=1.0,
            area_exact=1.0,
            area_rel_diff=0.0,
            dispo_equal=1.0,
            phash_match_ratio=0.9,
            interior_match_ratio=0.9,
            rare_token_overlap=3.0,
            containment_max=0.95,
            price_path_event_match=1.0,
        )
    )
    contradicted = model.predict_proba(
        feats(
            area_rel_diff=0.25,
            area_exact=0.0,
            attr_contradictions=3.0,
            numeral_conflict=1.0,
            floor_diff=3.0,
        )
    )
    assert nothing < 0.1
    assert strong > 0.9
    assert contradicted < nothing


def test_priors_penalise_catalogue_heavy_image_evidence() -> None:
    model = hand_initialised()
    clean = model.predict_proba(feats(phash_match_ratio=0.9, interior_match_ratio=0.9, catalog_ratio_max=0.0))
    catalogue = model.predict_proba(
        feats(phash_match_ratio=0.9, interior_match_ratio=0.9, catalog_ratio_max=0.9)
    )
    assert catalogue < clean
    plan_only = model.predict_proba(feats(plan_match_ratio=1.0))
    assert plan_only < model.predict_proba(feats(interior_match_ratio=1.0))


def test_overlap_is_penalised_only_inside_one_portal() -> None:
    """E7: a cross-portal advert live on both portals at once is the primary positive class."""
    model = hand_initialised()
    base = feats(phash_match_ratio=0.9, same_ruian_adm_kod=1.0, overlap_days=0.0, same_source=0.0)
    cross_portal = feats(
        phash_match_ratio=0.9, same_ruian_adm_kod=1.0, overlap_days=600.0, same_source=0.0
    )
    one_portal = feats(
        phash_match_ratio=0.9, same_ruian_adm_kod=1.0, overlap_days=600.0, same_source=1.0
    )
    assert model.predict_proba(cross_portal) == pytest.approx(model.predict_proba(base))
    assert model.score(one_portal) < model.score(cross_portal)


def test_gap_days_is_never_a_penalty() -> None:
    model = hand_initialised()
    fresh = model.predict_proba(feats(phash_match_ratio=0.9, same_ruian_adm_kod=1.0, gap_days=0.0))
    old = model.predict_proba(feats(phash_match_ratio=0.9, same_ruian_adm_kod=1.0, gap_days=900.0))
    assert old == pytest.approx(fresh)  # E6


def test_absence_is_scored_separately_from_a_zero_value() -> None:
    model = hand_initialised()
    model.presence_weights["dist_norm"] = 0.7
    known_far = model.predict_proba(feats(dist_norm=2.0))
    unknown = model.predict_proba({"dist_norm": (0.0, False)})
    assert known_far != unknown  # E12: (0.0, False) is not (0.0, True)


def _synthetic(count: int, seed: int) -> tuple[list[dict[str, tuple[float, bool]]], list[int]]:
    """A planted signal: duplicates share photos and an address and differ little in area."""
    rng = random.Random(seed)
    rows: list[dict[str, tuple[float, bool]]] = []
    labels: list[int] = []
    for index in range(count):
        positive = index % 2 == 0
        if positive:
            row = feats(
                area_rel_diff=abs(rng.gauss(0.01, 0.01)),
                phash_match_ratio=min(1.0, max(0.0, rng.gauss(0.75, 0.2))),
                same_ruian_adm_kod=1.0 if rng.random() < 0.7 else 0.0,
                jaccard_shingle=min(1.0, max(0.0, rng.gauss(0.6, 0.2))),
                n_images_min=float(rng.randint(1, 15)),
            )
        else:
            row = feats(
                area_rel_diff=abs(rng.gauss(0.12, 0.06)),
                phash_match_ratio=min(1.0, max(0.0, rng.gauss(0.08, 0.1))),
                same_ruian_adm_kod=1.0 if rng.random() < 0.15 else 0.0,
                jaccard_shingle=min(1.0, max(0.0, rng.gauss(0.25, 0.2))),
                n_images_min=float(rng.randint(1, 15)),
            )
        # Missingness is itself predictive: duplicates far more often carry a price path.
        if rng.random() < (0.8 if positive else 0.2):
            row["price_path_event_match"] = (1.0 if positive else rng.random() * 0.3, True)
        else:
            row["price_path_event_match"] = ft.ABSENT
        rows.append(row)
        labels.append(1 if positive else 0)
    return rows, labels


def test_fit_recovers_a_planted_signal_on_held_out_rows() -> None:
    order = (
        "area_rel_diff",
        "phash_match_ratio",
        "same_ruian_adm_kod",
        "jaccard_shingle",
        "n_images_min",
        "price_path_event_match",
    )
    model = LogisticModel(
        feature_order=order,
        weights={name: 0.0 for name in order},
        presence_weights={name: 0.0 for name in order},
        intercept=0.0,
        interactions=[("phash_match_ratio", "same_ruian_adm_kod", 0.0)],
    )
    train_rows, train_labels = _synthetic(600, seed=11)
    test_rows, test_labels = _synthetic(400, seed=29)
    model.fit(train_rows, train_labels, epochs=400, lr=0.5, l2=1e-4)

    scores = [model.predict_proba(row) for row in test_rows]
    assert auc(scores, test_labels) > 0.95
    assert model.weights["area_rel_diff"] < 0.0
    assert model.weights["phash_match_ratio"] > 0.0
    assert model.scales["n_images_min"] > 1.0  # standardisation is stored, not assumed
    assert model.means["phash_match_ratio"] != 0.0
    assert all(0.0 <= score <= 1.0 for score in scores)


def test_fit_learns_a_presence_term_when_only_presence_carries_the_signal() -> None:
    order = ("dist_norm",)
    model = LogisticModel(
        feature_order=order,
        weights={"dist_norm": 0.0},
        presence_weights={"dist_norm": 0.0},
        intercept=0.0,
        interactions=[],
    )
    rows: list[dict[str, tuple[float, bool]]] = []
    labels: list[int] = []
    for index in range(400):
        positive = index % 2 == 0
        rows.append({"dist_norm": (0.5, True) if positive else ft.ABSENT})
        labels.append(1 if positive else 0)
    model.fit(rows, labels, epochs=300, lr=0.5)
    assert model.presence_weights["dist_norm"] > 0.3
    assert model.predict_proba({"dist_norm": (0.5, True)}) > model.predict_proba(
        {"dist_norm": ft.ABSENT}
    )


def test_fit_validates_its_inputs() -> None:
    model = hand_initialised()
    with pytest.raises(ValueError):
        model.fit([{}], [0, 1])
    with pytest.raises(ValueError):
        model.fit([], [])
    with pytest.raises(ValueError):
        model.fit([{}, {}], [0, 1], sample_weights=[1.0])


def test_sample_weights_shift_the_fit_toward_the_weighted_rows() -> None:
    order = ("phash_match_ratio",)
    rows = [feats(phash_match_ratio=value) for value in (0.9, 0.9, 0.1, 0.1)]
    labels = [1, 0, 0, 1]

    def fitted(weights: list[float]) -> float:
        model = LogisticModel(
            feature_order=order,
            weights={"phash_match_ratio": 0.0},
            presence_weights={"phash_match_ratio": 0.0},
            intercept=0.0,
            interactions=[],
        )
        model.fit(rows, labels, epochs=300, lr=0.5, sample_weights=weights)
        return model.weights["phash_match_ratio"]

    assert fitted([5.0, 1.0, 5.0, 1.0]) > 0.0  # the weighted rows say "photos mean duplicate"
    assert fitted([1.0, 5.0, 1.0, 5.0]) < 0.0


def test_calibrate_reports_ece_and_stores_a_monotone_table() -> None:
    model = hand_initialised()
    # Deliberately overconfident scores: the empirical rate in each bin is half the stated one.
    probs: list[float] = []
    labels: list[int] = []
    for bin_index in range(10):
        stated = (bin_index + 0.5) / 10.0
        for row in range(20):
            probs.append(stated)
            labels.append(1 if row < round(20 * stated / 2.0) else 0)
    error = model.calibrate(probs, labels, bins=10)
    assert error == pytest.approx(expected_calibration_error(probs, labels, 10))
    assert error > 0.05
    assert model.calibration is not None
    values = [value for _, value in model.calibration]
    assert values == sorted(values)
    assert model.apply_calibration(0.95) < 0.95
    calibrated = [model.apply_calibration(prob) for prob in probs]
    assert expected_calibration_error(calibrated, labels, 10) < error


def test_calibration_applies_inside_predict_proba() -> None:
    model = hand_initialised()
    row = feats(same_ruian_adm_kod=1.0, phash_match_ratio=0.9)
    raw = model.predict_proba(row)
    model.calibration = [(0.2, 0.05), (0.8, 0.60)]
    calibrated = model.predict_proba(row)
    assert calibrated != raw
    assert 0.05 <= calibrated <= 0.60
    # Outside the knots the map clamps; inside it interpolates, so 0.5 sits halfway.
    assert model.apply_calibration(0.1) == pytest.approx(0.05, abs=1e-5)
    assert model.apply_calibration(0.9) == pytest.approx(0.60, abs=1e-5)
    assert model.apply_calibration(0.5) == pytest.approx(0.325, abs=1e-5)


def test_json_round_trip_reproduces_scores_exactly() -> None:
    order = ("area_rel_diff", "phash_match_ratio", "same_ruian_adm_kod")
    model = LogisticModel(
        feature_order=order,
        weights={name: 0.0 for name in order},
        presence_weights={name: 0.0 for name in order},
        intercept=0.0,
        interactions=[("phash_match_ratio", "same_ruian_adm_kod", 0.0)],
    )
    rows, labels = _synthetic(200, seed=3)
    trimmed = [{name: row[name] for name in order if name in row} for row in rows]
    model.fit(trimmed, labels, epochs=200, lr=0.5)
    model.calibrate([model.predict_proba(row) for row in trimmed], labels)

    payload = json.loads(json.dumps(model.to_json()))
    clone = LogisticModel.from_json(payload)
    assert clone.to_json() == model.to_json()
    for row in trimmed:
        assert clone.predict_proba(row) == model.predict_proba(row)
    assert LogisticModel.from_json(json.dumps(payload)).intercept == model.intercept


def test_contributions_decompose_the_score() -> None:
    model = hand_initialised()
    row = feats(same_ruian_adm_kod=1.0, area_rel_diff=0.02, phash_match_ratio=0.5)
    parts = model.contributions(row)
    total = model.intercept + sum(parts.values())
    assert total == pytest.approx(model.score(row))
    assert sigmoid(model.score(row)) == pytest.approx(model.predict_proba(row))
    assert parts["same_ruian_adm_kod"] > 0.0
    assert parts["area_rel_diff"] < 0.0
    assert parts["area_rel_diff:present"] > 0.0  # knowing both areas is its own (E12) term


def test_unknown_features_in_a_row_are_ignored_and_missing_ones_read_as_absent() -> None:
    model = hand_initialised()
    base = model.predict_proba(feats(same_ruian_adm_kod=1.0))
    noisy = model.predict_proba(feats(same_ruian_adm_kod=1.0, not_a_feature=5.0))
    assert base == noisy


def test_fit_converges_and_reports_its_convergence() -> None:
    """E21 reads T_hi/T_lo off calibrated probabilities, so a shrunk (under-fit) weight vector is
    a silent threshold error: `fit` iterates to a gradient tolerance and says whether it got there."""
    order = ("x1", "x2", "x3", "x4")
    planted = {"x1": 2.5, "x2": -1.5, "x3": 0.0, "x4": 0.8}
    rng = random.Random(5)
    rows: list[dict[str, tuple[float, bool]]] = []
    labels: list[int] = []
    for _ in range(1200):
        row = {name: (rng.gauss(0.0, 1.0), True) for name in order}
        z = -0.4 + sum(planted[name] * row[name][0] for name in order)
        rows.append(row)
        labels.append(1 if rng.random() < 1.0 / (1.0 + math.exp(-z)) else 0)
    model = LogisticModel(
        feature_order=order,
        weights={name: 0.0 for name in order},
        presence_weights={name: 0.0 for name in order},
        intercept=0.0,
        interactions=[],
    )
    model.fit(rows, labels, l2=1e-4)
    assert model.fit_report["mean_abs_grad"] < 1e-4
    assert model.fit_report["log_loss"] > 0.0
    for name in ("x1", "x2", "x4"):
        assert model.weights[name] == pytest.approx(planted[name], rel=0.25), name
    assert abs(model.weights["x3"]) < 0.3
    probs = [model.predict_proba(row) for row in rows]
    assert auc(probs, labels) > 0.9
    assert expected_calibration_error(probs, labels, 10) < 0.05


def test_calibration_is_piecewise_linear_not_a_step_per_bin() -> None:
    model = hand_initialised()
    rng = random.Random(7)
    probs: list[float] = []
    labels: list[int] = []
    for _ in range(1000):
        stated = rng.random()
        probs.append(stated)
        labels.append(1 if rng.random() < stated / 2.0 else 0)
    model.calibrate(probs, labels, bins=10)
    calibrated = [model.apply_calibration(prob) for prob in probs]
    assert len(set(calibrated)) > 10  # ranking survives: not one value per bin
    ordered = sorted(set(probs))
    for left, right in zip(ordered, ordered[1:]):
        assert model.apply_calibration(left) < model.apply_calibration(right)
    assert expected_calibration_error(calibrated, labels, 10) < expected_calibration_error(
        probs, labels, 10
    )


def test_fit_refuses_an_interaction_naming_an_unknown_feature() -> None:
    """A renamed feature must fail loudly, never train a coefficient onto the wrong pair."""
    order = ("f1", "f2", "f3")
    model = LogisticModel(
        feature_order=order,
        weights={name: 0.0 for name in order},
        presence_weights={name: 0.0 for name in order},
        intercept=0.0,
        interactions=[("f1", "not_a_feature", 9.0), ("f2", "f3", 0.0)],
    )
    rows = [feats(f1=1.0, f2=1.0, f3=1.0), feats(f1=0.0, f2=0.0, f3=0.0)]
    with pytest.raises(ValueError, match="not_a_feature"):
        model.fit(rows, [1, 0], epochs=10)


def test_fit_keeps_every_interaction_it_was_given() -> None:
    order = ("f1", "f2", "f3")
    model = LogisticModel(
        feature_order=order,
        weights={name: 0.0 for name in order},
        presence_weights={name: 0.0 for name in order},
        intercept=0.0,
        interactions=[("f1", "f2", 0.0), ("f2", "f3", 0.0)],
    )
    rows = [feats(f1=1.0, f2=1.0, f3=0.0), feats(f1=0.0, f2=1.0, f3=1.0)] * 20
    labels = [1, 0] * 20
    model.fit(rows, labels, epochs=50, lr=0.2)
    assert [(left, right) for left, right, _ in model.interactions] == [("f1", "f2"), ("f2", "f3")]


# --- IRLS: the Newton optimiser -----------------------------------------------------------


def _planted(n: int, seed: int, flip: float = 0.02) -> tuple[list[dict[str, Any]], list[int]]:
    """A separable-ish design: a deterministic boundary plus a few flipped labels."""
    order = ("x1", "x2", "x3")
    planted = {"x1": 3.0, "x2": -2.0, "x3": 0.0}
    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []
    labels: list[int] = []
    for _ in range(n):
        row = {name: (rng.gauss(0.0, 1.0), True) for name in order}
        z = -0.2 + sum(planted[name] * row[name][0] for name in order)
        label = 1 if z > 0.0 else 0
        if rng.random() < flip:
            label = 1 - label
        rows.append(row)
        labels.append(label)
    return rows, labels


def _blank(order: tuple[str, ...], interactions: list[tuple[str, str, float]] | None = None
           ) -> LogisticModel:
    return LogisticModel(
        feature_order=order,
        weights={name: 0.0 for name in order},
        presence_weights={name: 0.0 for name in order},
        intercept=0.0,
        interactions=list(interactions or []),
    )


def test_fit_irls_recovers_the_planted_weights_in_a_handful_of_newton_steps() -> None:
    """Gradient descent is still crawling at its epoch ceiling here; Newton lands on the optimum."""
    rows, labels = _planted(800, seed=17)
    model = _blank(("x1", "x2", "x3"))
    model.fit_irls(rows, labels, l2=1e-3)

    assert model.fit_report["converged"] == 1.0
    assert model.fit_report["iterations"] < 30
    assert model.fit_report["max_abs_delta"] <= 1e-8
    assert model.weights["x1"] > 0.0 and model.weights["x2"] < 0.0
    assert abs(model.weights["x3"]) < 0.3
    ratio = model.weights["x1"] / model.weights["x2"]
    assert ratio == pytest.approx(3.0 / -2.0, rel=0.25)
    probs = [model.predict_proba(row) for row in rows]
    assert auc(probs, labels) > 0.98

    slow = _blank(("x1", "x2", "x3"))
    slow.fit(rows, labels, l2=1e-3)
    assert slow.fit_report["converged"] == 0.0  # the contrast the default rests on
    assert model.fit_report["log_loss"] <= slow.fit_report["log_loss"] + 1e-9


def test_fit_irls_matches_the_direction_gradient_descent_takes_on_the_same_data() -> None:
    order = ("x1", "x2", "x3")
    rows, labels = _planted(400, seed=23)
    newton = _blank(order)
    newton.fit_irls(rows, labels, l2=1e-2)
    descent = _blank(order)
    descent.fit(rows, labels, l2=1e-2, epochs=4000, lr=0.5)
    for name in order:
        if abs(descent.weights[name]) < 0.05:
            continue
        assert math.copysign(1.0, newton.weights[name]) == math.copysign(
            1.0, descent.weights[name]
        ), name
    assert math.copysign(1.0, newton.intercept) == math.copysign(1.0, descent.intercept)
    assert newton.fit_report["log_loss"] <= descent.fit_report["log_loss"] + 1e-6


def test_fit_irls_survives_a_constant_column_and_an_unpenalised_singular_system() -> None:
    """A zero-variance feature (and a presence flag collinear with the intercept) has no finite
    Newton step: the solver pins it at zero instead of dividing by numerical noise."""
    order = ("constant", "signal", "always_present")
    rows: list[dict[str, Any]] = []
    labels: list[int] = []
    rng = random.Random(3)
    for index in range(200):
        positive = index % 2 == 0
        rows.append({
            "constant": (1.0, True),
            "signal": (1.0 if positive else 0.0, True),
            "always_present": (0.0, True),
            "never_present": ft.ABSENT,
        })
        labels.append(1 if positive else 0)
        rng.random()
    for l2 in (1e-3, 0.0):
        model = _blank(order)
        model.fit_irls(rows, labels, l2=l2, max_iter=30)
        values = list(model.weights.values()) + list(model.presence_weights.values())
        values.append(model.intercept)
        assert all(math.isfinite(value) for value in values), (l2, values)
        assert model.weights["constant"] == 0.0  # standardised to a zero column
        assert model.scales["constant"] == 1.0
        assert model.weights["signal"] > 0.0
        # Dropping the unidentified columns is what keeps the system non-singular at l2=0: the
        # solver never has to pin anything, so no equation is silently discarded.
        assert model.presence_weights["always_present"] == 0.0
        assert model.fit_report["pinned_terms"] == 0.0
        assert model.fit_report["terms_dropped"] >= 4.0
        probability = model.predict_proba(rows[0])
        assert 0.0 <= probability <= 1.0 and math.isfinite(probability)


def test_fit_irls_honours_sample_weights() -> None:
    order = ("phash_match_ratio",)
    rows = [feats(phash_match_ratio=value) for value in (0.9, 0.9, 0.1, 0.1)]
    labels = [1, 0, 0, 1]

    def fitted(weights: list[float]) -> float:
        model = _blank(order)
        model.fit_irls(rows, labels, sample_weights=weights)
        return model.weights["phash_match_ratio"]

    assert fitted([5.0, 1.0, 5.0, 1.0]) > 0.0
    assert fitted([1.0, 5.0, 1.0, 5.0]) < 0.0
    assert fitted([1.0, 1.0, 1.0, 1.0]) == pytest.approx(0.0, abs=1e-6)


def test_fit_irls_validates_its_inputs_like_the_gradient_fit() -> None:
    model = hand_initialised()
    with pytest.raises(ValueError):
        model.fit_irls([{}], [0, 1])
    with pytest.raises(ValueError):
        model.fit_irls([], [])
    with pytest.raises(ValueError):
        model.fit_irls([{}, {}], [0, 1], sample_weights=[1.0])
    renamed = _blank(("f1", "f2"), [("f1", "not_a_feature", 0.0)])
    with pytest.raises(ValueError, match="not_a_feature"):
        renamed.fit_irls([feats(f1=1.0, f2=1.0), feats(f1=0.0, f2=0.0)], [1, 0])
    with pytest.raises(ValueError, match="max_iter"):
        model.fit_irls([feats(f1=1.0)], [1], max_iter=0)


def test_fit_irls_produces_the_same_artifact_shape_as_the_gradient_fit() -> None:
    order = ("x1", "x2", "x3")
    rows, labels = _planted(300, seed=41)
    interactions = [("x1", "x2", 0.0)]
    newton = _blank(order, interactions)
    newton.fit_irls(rows, labels)
    descent = _blank(order, interactions)
    descent.fit(rows, labels, epochs=50)

    assert set(newton.to_json()) == set(descent.to_json())
    assert set(newton.weights) == set(order) and set(newton.presence_weights) == set(order)
    assert [(left, right) for left, right, _ in newton.interactions] == [("x1", "x2")]
    assert set(newton.fit_report) == {
        "iterations", "max_abs_delta", "log_loss", "converged",
        "terms", "terms_identified", "terms_dropped", "duplicate_terms",
        "pinned_terms", "gradient_fallbacks",
    }
    probs = [newton.predict_proba(row) for row in rows]
    newton.calibrate(probs, labels)
    restored = LogisticModel.from_json(json.loads(json.dumps(newton.to_json())))
    for row in rows[:20]:
        assert restored.predict_proba(row) == newton.predict_proba(row)


def test_fit_irls_reports_a_ceiling_it_did_not_reach() -> None:
    rows, labels = _planted(300, seed=53)
    model = _blank(("x1", "x2", "x3"))
    model.fit_irls(rows, labels, max_iter=1)
    assert model.fit_report["iterations"] == 1.0
    assert model.fit_report["converged"] == 0.0
    assert model.fit_report["max_abs_delta"] > 1e-8


def test_fit_irls_learns_a_presence_term_when_only_presence_carries_the_signal() -> None:
    model = _blank(("dist_norm",))
    rows: list[dict[str, Any]] = []
    labels: list[int] = []
    for index in range(400):
        positive = index % 2 == 0
        rows.append({"dist_norm": (0.5, True) if positive else ft.ABSENT})
        labels.append(1 if positive else 0)
    model.fit_irls(rows, labels, l2=1e-3)
    assert model.presence_weights["dist_norm"] > 0.3
    assert model.predict_proba({"dist_norm": (0.5, True)}) > model.predict_proba(
        {"dist_norm": ft.ABSENT}
    )


def test_fit_irls_refuses_to_certify_a_step_the_line_search_never_took(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Halving shrinks a REJECTED step under any tolerance; only an ACCEPTED step is convergence."""
    rows, labels = _planted(200, seed=61)
    model = _blank(("x1", "x2", "x3"))

    def overshoot(matrix: Any, rhs: Any) -> tuple[list[float], list[int]]:
        return [value * 1e9 for value in rhs], []

    monkeypatch.setattr(model_module, "_solve_linear_system", overshoot)
    monkeypatch.setattr(model_module, "IRLS_MAX_HALVINGS", 2)
    model.fit_irls(rows, labels, max_iter=3)
    assert model.fit_report["converged"] == 0.0
    assert model.fit_report["max_abs_delta"] > 1e-8
    assert model.intercept == 0.0 and model.weights["x1"] == 0.0  # never moved off the warm start


def test_fit_irls_falls_back_to_steepest_descent_on_an_uphill_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rank-deficient solve can point uphill; the fit must descend anyway and say it did so."""
    rows, labels = _planted(300, seed=67)
    model = _blank(("x1", "x2", "x3"))
    real = model_module._solve_linear_system

    def uphill(matrix: Any, rhs: Any) -> tuple[list[float], list[int]]:
        step, pinned = real(matrix, rhs)
        return [-value for value in step], pinned

    monkeypatch.setattr(model_module, "_solve_linear_system", uphill)
    model.fit_irls(rows, labels, max_iter=40)
    assert model.fit_report["gradient_fallbacks"] > 0.0
    assert model.weights["x1"] > 0.0 and model.weights["x2"] < 0.0
    assert model.fit_report["log_loss"] < 0.693


def test_solve_linear_system_names_the_equation_it_discards() -> None:
    """A degenerate pivot DROPS that row: an inconsistent system must not come back as a solve."""
    solution, pinned = model_module._solve_linear_system(
        [[2.0, 1.0, 1.0], [1.0, 1.0, 1.0], [1.0, 1.0, 1.0]], [3.0, 2.0, 5.0]
    )
    assert pinned == [2]
    assert all(math.isfinite(value) for value in solution)

    redundant, free = model_module._solve_linear_system([[2.0, 1.0], [1.0, 0.5]], [3.0, 1.5])
    assert free == []  # a consistent duplicate equation costs nothing to drop
    assert all(math.isfinite(value) for value in redundant)

    with pytest.raises(ValueError, match="non-finite"):
        model_module._solve_linear_system([[float("nan"), 0.0], [0.0, 1.0]], [1.0, 1.0])


def test_fit_irls_drops_unidentified_terms_and_names_the_collinear_ones() -> None:
    """An always-on presence flag IS the intercept; ridge would split one effect across copies."""
    order = ("signal", "twin_a", "twin_b")
    rows: list[dict[str, Any]] = []
    labels: list[int] = []
    rng = random.Random(11)
    for index in range(240):
        value = rng.gauss(0.0, 1.0)
        together = index % 2 == 0
        row: dict[str, Any] = {"signal": (value, True)}
        if together:
            row["twin_a"] = (1.0, True)
            row["twin_b"] = (1.0, True)
        else:
            row["twin_a"] = ft.ABSENT
            row["twin_b"] = ft.ABSENT
        rows.append(row)
        labels.append(1 if value > 0.0 else 0)
    model = _blank(order)
    report = model.fit_irls(rows, labels, l2=1e-3)

    assert "signal:present" in report["dropped_terms"]
    assert model.presence_weights["signal"] == 0.0
    assert any(
        {"twin_a:present", "twin_b:present"} <= set(group)
        for group in report["duplicate_groups"]
    )
    assert report["terms_identified"] + len(report["dropped_terms"]) == report["terms"]
    assert model.fit_report["terms_identified"] == float(report["terms_identified"])
    assert model.fit_report["duplicate_terms"] > 0.0
    assert model.weights["signal"] > 0.0


# --- Platt calibration (W4c) -----------------------------------------------------------------


def _platt_sample(n: int = 400) -> tuple[list[float], list[int]]:
    """Scores whose TRUE rate is the square of the predicted probability: over-confident at the
    top, the shape a merge model is punished for."""
    rng = random.Random(20260916)
    probs = [(index + 0.5) / n for index in range(n)]
    labels = [1 if rng.random() < prob ** 2 else 0 for prob in probs]
    return probs, labels


def test_platt_calibration_is_monotone_and_pulls_the_ece_down() -> None:
    probs, labels = _platt_sample()
    model = hand_initialised()
    before = model.calibrate(probs, labels, method="platt")
    mapped = [model.apply_calibration(prob) for prob in probs]
    after = expected_calibration_error(mapped, labels)
    assert model.calibration_kind == "platt"
    assert len(model.calibration) == model_module.PLATT_GRID_POINTS
    assert after < before
    # strictly increasing: a calibration that re-orders the merge queue is not a calibration
    assert all(right > left for left, right in zip(mapped, mapped[1:]))


def test_platt_is_two_parameters_where_isotonic_is_one_per_bin() -> None:
    probs, labels = _platt_sample()
    platt = hand_initialised()
    platt.calibrate(probs, labels, method="platt")
    isotonic = hand_initialised()
    isotonic.calibrate(probs, labels, method="isotonic")
    assert isotonic.calibration_kind == "isotonic"
    # Both map the same fold; they are different maps, and the artifact says which one it is.
    assert platt.calibration != isotonic.calibration
    assert platt.to_json()["calibration_kind"] == "platt"
    assert LogisticModel.from_json(platt.to_json()).calibration_kind == "platt"


def test_platt_survives_a_separable_fold_and_a_constant_one() -> None:
    separable = [0.1] * 20 + [0.9] * 20
    labels = [0] * 20 + [1] * 20
    model = hand_initialised()
    model.calibrate(separable, labels, method="platt")
    assert model.calibration_kind == "platt"
    assert model.apply_calibration(0.9) > model.apply_calibration(0.1)
    # one predicted value for both classes: no slope to fit, and nothing may be claimed
    flat = hand_initialised()
    flat.calibrate([0.5] * 20, [1, 0] * 10, method="platt")
    assert flat.calibration is None or flat.calibration_kind == "platt"


def test_calibrate_refuses_a_map_it_does_not_implement() -> None:
    model = hand_initialised()
    with pytest.raises(ValueError):
        model.calibrate([0.2, 0.8], [0, 1], method="beta")


def test_a_refit_drops_the_calibration_and_its_provenance() -> None:
    probs, labels = _platt_sample(n=60)
    model = hand_initialised()
    model.calibrate(probs, labels, method="platt")
    rows = [{"area_rel_diff": (0.01, True), "tfidf_cos": (0.9, True)} for _ in range(20)]
    model.fit_irls(rows, [1, 0] * 10, max_iter=3)
    assert model.calibration is None and model.calibration_kind is None


# --- the calibration map must not be the thing that caps the merge zone ----------------------


def _saturated_tail() -> tuple[list[float], list[int]]:
    """A fold whose top decile is purer than the two top deciles pooled together.

    Bin 9 outscores bin 10, so PAV over fixed bins pools the two and the map's ceiling becomes
    their JOINT rate — even though the top of bin 10 is flawless."""
    probs = [0.05 * (index % 10) for index in range(60)]
    labels = [0] * 60
    probs += [0.860 + 0.001 * index for index in range(30)]     # bin 9: 29 of 30
    labels += [0] + [1] * 29
    probs += [0.900 + 0.001 * index for index in range(30)]     # bin 10, lower: 26 of 30
    labels += [0] * 4 + [1] * 26
    probs += [0.970 + 0.0005 * index for index in range(30)]    # bin 10, upper: flawless
    labels += [1] * 30
    return probs, labels


def test_ten_fixed_bins_cap_the_map_below_a_tail_the_data_can_separate() -> None:
    probs, labels = _saturated_tail()
    binned = hand_initialised()
    binned.calibrate(probs, labels, method="isotonic")
    pav = hand_initialised()
    pav.calibrate(probs, labels, method="isotonic_pav")
    assert pav.calibration_kind == "isotonic_pav"
    top = binned.apply_calibration(0.999999)
    # PAV over the distinct scores keeps the flawless top separable; the binned map pools it
    assert pav.apply_calibration(0.999999) > top
    assert binned.apply_calibration(0.88) == pytest.approx(top, abs=1e-3)
    assert pav.apply_calibration(0.88) < pav.apply_calibration(0.999999)
    # both stay monotone, which is the property every downstream ordering depends on
    for mapping in (binned, pav):
        values = [mapping.apply_calibration(value / 50.0) for value in range(51)]
        assert values == sorted(values)


def test_isotonic_over_observations_puts_ties_on_one_anchor() -> None:
    model = hand_initialised()
    model.calibrate([0.4, 0.4, 0.4, 0.9, 0.9], [0, 1, 1, 1, 1], method="isotonic_pav")
    assert model.calibration is not None
    anchors = [anchor for anchor, _ in model.calibration]
    assert anchors == sorted(set(anchors))
    assert len(anchors) == 2
    assert model.apply_calibration(0.4) == pytest.approx(2.0 / 3.0, abs=1e-3)


def test_a_calibration_map_can_be_fitted_on_the_design_not_the_head_count() -> None:
    probs = [0.9] * 10 + [0.9] * 10
    labels = [1] * 10 + [0] * 10
    heads = hand_initialised()
    heads.calibrate(probs, labels, method="isotonic_pav")
    designed = hand_initialised()
    designed.calibrate(probs, labels, method="isotonic_pav",
                       weights=[9.0] * 10 + [1.0] * 10)
    assert heads.apply_calibration(0.9) == pytest.approx(0.5, abs=1e-3)
    assert designed.apply_calibration(0.9) == pytest.approx(0.9, abs=1e-3)
    # the binned map and the sigmoid take the same weights
    binned = hand_initialised()
    binned.calibrate(probs, labels, method="isotonic", weights=[9.0] * 10 + [1.0] * 10)
    assert binned.apply_calibration(0.9) == pytest.approx(0.9, abs=1e-3)
    platt = hand_initialised()
    assert platt.calibrate(
        [0.1] * 10 + [0.9] * 10, [0] * 10 + [1] * 10, method="platt",
        weights=[1.0] * 20,
    ) >= 0.0


def test_calibrate_refuses_a_weight_per_row_it_did_not_get() -> None:
    model = hand_initialised()
    with pytest.raises(ValueError, match="length mismatch"):
        model.calibrate([0.2, 0.8], [0, 1], weights=[1.0])


def test_the_expected_calibration_error_can_be_read_over_design_weights() -> None:
    probs = [0.9] * 10 + [0.9] * 10
    labels = [1] * 10 + [0] * 10
    assert expected_calibration_error(probs, labels) == pytest.approx(0.4, abs=1e-6)
    weighted = expected_calibration_error(probs, labels, 10, [9.0] * 10 + [1.0] * 10)
    assert weighted == pytest.approx(0.0, abs=1e-6)
    with pytest.raises(ValueError, match="length mismatch"):
        expected_calibration_error(probs, labels, 10, [1.0])


# --- provenance travels WITH the artifact ----------------------------------------------------


def test_provenance_survives_the_json_round_trip() -> None:
    model = hand_initialised()
    model.provenance = {
        "feature_version": {"sha256_16": model.feature_digest()},
        "seal": {"sha256": "abc"},
        "label_provenance": {"files": ["j.jsonl"], "n": 3},
    }
    body = model.to_json()
    assert body["provenance"]["seal"]["sha256"] == "abc"
    restored = LogisticModel.from_json(json.dumps(body))
    assert restored.provenance == model.provenance
    # a model with nothing to say does not grow an empty key
    assert "provenance" not in hand_initialised().to_json()


def test_a_model_fitted_against_another_feature_order_refuses_to_load() -> None:
    model = hand_initialised()
    model.provenance = {"feature_version": {"sha256_16": "0123456789abcdef"}}
    with pytest.raises(ValueError, match="refit before scoring"):
        LogisticModel.from_json(model.to_json())
    # the check is the default, not the option: reading one anyway has to be asked for
    loose = LogisticModel.from_json(model.to_json(), strict=False)
    assert loose.provenance["feature_version"]["sha256_16"] == "0123456789abcdef"


def test_the_feature_digest_moves_when_the_vocabulary_does() -> None:
    assert model_module.feature_order_digest(("a", "b")) != model_module.feature_order_digest(
        ("b", "a")
    )
    assert len(model_module.feature_order_digest(("a",))) == model_module.FEATURE_DIGEST_CHARS
    assert hand_initialised().feature_digest() == model_module.feature_order_digest(
        ft.FEATURE_ORDER
    )


# --- the stamp is tautological; the CODE is the other half of the check ------------------------

# The v4 tail (W5): twelve room-paired slots no shipped model is fitted on.
W5_TAIL: tuple[str, ...] = ft.TAG_FEATURE_NAMES + ("floor_stated_conflict",)

# The v5 tail (W8, E60): the shared agency order code. No shipped model scores it yet — the
# certificate reads it directly, so the debt costs precision, not the rule.
W8_TAIL: tuple[str, ...] = ("ref_code_shared",)

# The feature names `autodedup/models/w4_gold.json` was fitted WITHOUT and therefore scores at
# zero. Empty is the goal: the next refit must shrink this tuple to () in the same commit that
# reruns the fit, and this test is the place the debt is written down.
W4_GOLD_UNSCORED: tuple[str, ...] = (
    ("plot_area_rel_diff", "plot_area_exact") + W5_TAIL + W8_TAIL
)


def test_the_shipped_model_names_the_features_it_cannot_score() -> None:
    """`check_provenance` compares the artifact's stamp against the artifact's own feature_order,
    so it is blind to a feature added to the CODE afterwards — a 45-name model loads silently
    against 47-name code and scores the tail at zero. `check_feature_order` is the other half."""
    body = json.loads((ROOT / "autodedup" / "models" / "w4_gold.json").read_text())
    stored = tuple(body["feature_order"])
    assert ft.FEATURE_ORDER[: len(stored)] == stored  # grows at the END only
    assert ft.FEATURE_ORDER[len(stored) :] == W4_GOLD_UNSCORED
    with pytest.warns(UserWarning, match="refit to let them pay") as warned:
        LogisticModel.from_json(json.dumps(body))
    assert all(name in str(warned[0].message) for name in W4_GOLD_UNSCORED)


def test_a_renamed_feature_is_a_load_error_not_a_warning() -> None:
    model = hand_initialised()
    model.feature_order = ("area_rel_diff_OLD",) + ft.FEATURE_ORDER[1:]
    model.provenance = {"feature_version": {"sha256_16": model.feature_digest()}}
    with pytest.raises(ValueError, match="no longer defines"):
        LogisticModel.from_json(json.dumps(model.to_json()))


def test_a_model_fitted_on_the_current_order_warns_about_nothing() -> None:
    model = hand_initialised()
    model.provenance = {"feature_version": {"sha256_16": model.feature_digest()}}
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        LogisticModel.from_json(json.dumps(model.to_json()))


def test_an_unstamped_hand_model_over_a_feature_subset_is_exempt() -> None:
    """A fit stamps `feature_version`; a model built by hand over three features for a test or a
    probe does not, and must not be nagged about the 44 it deliberately left out."""
    order = ("area_rel_diff", "phash_match_ratio", "same_ruian_adm_kod")
    model = LogisticModel(
        feature_order=order,
        weights={name: 0.0 for name in order},
        presence_weights={name: 0.0 for name in order},
        intercept=0.0,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        LogisticModel.from_json(json.dumps(model.to_json()))


def test_the_w4f_refit_pays_the_debt_the_w4_model_still_carries() -> None:
    """W4e wrote the debt down; W4f is the refit that cleared it FOR V3. W5 adds the twelve
    room-paired slots and the shipped model is NOT refit onto them (the refit measured worse on
    every precision column — see PROGRAM.md M27-M31), so `w4f_gold` now carries the SAME kind of
    debt `w4_gold` does and warns by name. What still holds is the shape: the order grew at the
    END only, so the shipped weights all still land on the features they were fitted on."""
    body = json.loads((ROOT / "autodedup" / "models" / "w4f_gold.json").read_text())
    stored = tuple(body["feature_order"])
    assert ft.FEATURE_ORDER[: len(stored)] == stored
    assert ft.FEATURE_ORDER[len(stored):] == W5_TAIL + W8_TAIL
    with pytest.warns(UserWarning, match="refit to let them pay") as warned:
        model = LogisticModel.from_json(json.dumps(body))
    assert all(name in str(warned[0].message) for name in W5_TAIL)
    assert model.version == "w4f_gold"
    # the two features W4e added carry a learned weight now, not a structural zero
    assert model.weights["plot_area_exact"] != 0.0
    assert model.weights["plot_area_rel_diff"] != 0.0


def test_w5_gold_is_the_v4_refit_and_pays_the_room_paired_slots() -> None:
    """W5d promoted the v4 refit on the healed cohort (PROGRAM.md D16, generation g4).

    It was fitted on all 59 v4 slots. W8 appended a 60th (`ref_code_shared`, E60), so the
    artifact now carries a refit debt of exactly that one name — the warning IS the debt, and
    the test pins its size rather than pretending it is not there."""
    body = json.loads((ROOT / "autodedup" / "models" / "w5_gold.json").read_text())
    assert set(body["feature_order"]) == set(ft.FEATURE_ORDER) - {"ref_code_shared"}
    with warnings.catch_warnings(record=True) as warned:
        warnings.simplefilter("always")
        model = LogisticModel.from_json(json.dumps(body))
    assert len(warned) == 1 and "ref_code_shared" in str(warned[0].message)
    assert model.version == "w5_gold"
    assert all(model.weights[name] != 0.0 for name in W5_TAIL)
    # The seal is the W4f split map's: the fit reused it rather than re-randomising the holdout.
    assert body["provenance"]["seal"]["sha256"].startswith("ab2bd7eee78d")
    # `w5_gold` is a MEASUREMENT-only-gold artifact: the boundary draw it was judged against must
    # never appear in the fit corpus (a fit on labels picked from its own errors is adaptive).
    assert "35205840437" not in body["provenance"]["w5d"]["judgement_runs"]
