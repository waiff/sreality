"""The stdlib scorer: priors, standardised fit, calibration, and the JSON artifact round-trip."""

from __future__ import annotations

import json
import math
import random
from typing import Any

import pytest

from autodedup import features as ft
from autodedup.model import (
    LogisticModel,
    auc,
    expected_calibration_error,
    hand_initialised,
    sigmoid,
)


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
