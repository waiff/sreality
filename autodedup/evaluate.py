"""The W3 evaluation protocol (PROGRAM.md §9) and the first fit off real labels (§6).

Four things live here, in the order the program needs them.

**Interval arithmetic.** `wilson_interval` is the labelled-count cross-check, `clopper_pearson` the
exact one, and `stratified_bootstrap_ci` the design-based interval §9 metric 2 actually asks for —
the judge sample is drawn with per-stratum quotas and inclusion weights running 1x to ~500x, so a
binomial interval on those counts describes a sample nobody drew. The exact interval needs an
inverse regularized incomplete beta and this tree is stdlib-only, so `_betainc` is the Lentz
continued fraction and `_beta_quantile` bisects it to 1e-12 — chosen over the `math.comb` binomial
tail because the tail sum is O(n) per probe and costs precision at n in the thousands, where the
program's samples actually sit, while bisection is ~40 continued-fraction evaluations regardless.

**`evaluate`.** Precision of the merge zone against judge labels, split per stratum and per
deciding layer (K-A/K-B/K-C/model) because a certificate's precision is structural and a model's
is learned — pooling them hides which one is failing. The headline quantity is the Horvitz-Thompson
(Hajek) rate with a stratified-bootstrap interval; the unweighted rate rides alongside as the
labelled cross-check, never as the gate.

**`thresholds`.** T_hi is searched by SIMULATING the zone rule (`decide.decide_pair`), not by
pretending the merge zone is a score cut: a certificate merges a pair at any score, the E11
diversity gate demotes a non-diverse pair above T_hi, and an auto-reject drops one at any score.
T_lo is read off the HT-weighted positives below it — design weights only, never tier credibility —
and consumes tie groups whole, because the engine rejects every pair at the cut, not the one the
search happened to stop on. Both are fitted on train+validation and RE-MEASURED on the sealed test
split, which is the only number §6 lets anyone quote.

**`fit_model`.** 60/20/20 **by component, never by pair** — pairs inside one cluster are not
independent — over a group map that is stamped and re-usable, so a challenger model can be scored
on the incumbent's seal. Training weights are label credibility x design weight (capped at a
multiple of the median, with the realised design effect reported); the threshold search is not.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from autodedup.decide import MIN_EVIDENCE_FAMILIES
from autodedup.labels import (
    CHEAP_TIERS,
    EMPTY_SAMPLE,
    Label,
    PairKey,
    Sample,
    pair_key,
)
from autodedup.model import LogisticModel, auc, expected_calibration_error, hand_initialised
from autodedup.settings import Settings

Z_95: float = 1.96
ALPHA_95: float = 0.05

CERTIFICATE_CLASSES: tuple[str, ...] = ("K-A", "K-B", "K-C", "model")
MERGE_ZONE: str = "merge"
BAND_ZONE: str = "band"
REJECT_ZONE: str = "reject"
DECIDED_ZONES: tuple[str, ...] = (MERGE_ZONE, REJECT_ZONE)

# D3's per-stratum floor: a pooled 0.99 that hides a 0.90 stratum is not a passing gate.
STRATUM_FLOOR: float = 0.97

# A stratum weight is a population ratio and the real draw runs to ~500x on the thinnest cell.
# Capping at a constant would re-order the design (a 4,000-pair stratum and an 80-pair one would
# land within a factor of two), so the cap is a MULTIPLE OF THE MEDIAN weight and the realised
# design effect is reported next to it — the cap is visible, never silent.
WEIGHT_CAP_MEDIAN_MULTIPLE: float = 10.0

BOOTSTRAP_DRAWS: int = 2000
BOOTSTRAP_SEED: int = 20260916

SPLIT_SEED: int = 20260916
TRAIN_SHARE: int = 60
VALIDATION_SHARE: int = 20
DEV_SPLITS: tuple[str, ...] = ("train", "validation")
RELIABILITY_BINS: int = 10


# --- interval arithmetic -------------------------------------------------------------------


def wilson_interval(k: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval. For k == n it reduces to `n / (n + z^2)`, which is why a perfect
    sample needs n >= 381 to clear a 0.99 lower bound and n = 380 lands at 0.98999."""
    if n <= 0:
        return (0.0, 1.0)
    if k < 0 or k > n:
        raise ValueError(f"k must be in [0, n]: k={k} n={n}")
    rate = k / n
    denominator = 1.0 + z * z / n
    centre = (rate + z * z / (2 * n)) / denominator
    spread = z * math.sqrt(rate * (1.0 - rate) / n + z * z / (4 * n * n)) / denominator
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def wilson_lower(k: int, n: int, z: float = Z_95) -> float:
    return wilson_interval(k, n, z)[0]


def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _betacf(a: float, b: float, x: float, iterations: int = 300, tiny: float = 1e-30) -> float:
    """Modified Lentz continued fraction for the incomplete beta (NR §6.4)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, iterations + 1):
        m2 = 2 * m
        numerator = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + numerator * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + numerator / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        numerator = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + numerator * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + numerator / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(a * math.log(x) + b * math.log1p(-x) - _log_beta(a, b))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _beta_quantile(p: float, a: float, b: float, tol: float = 1e-12) -> float:
    """The inverse of `_betainc` in x, by bisection — I_x is strictly increasing in x."""
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    low, high = 0.0, 1.0
    for _ in range(200):
        middle = (low + high) / 2.0
        if _betainc(a, b, middle) < p:
            low = middle
        else:
            high = middle
        if high - low < tol:
            break
    return (low + high) / 2.0


def clopper_pearson(k: int, n: int, alpha: float = ALPHA_95) -> tuple[float, float]:
    """Exact (Clopper-Pearson) binomial interval via beta quantiles; k=0 pins the floor at 0."""
    if n <= 0:
        return (0.0, 1.0)
    if k < 0 or k > n:
        raise ValueError(f"k must be in [0, n]: k={k} n={n}")
    low = 0.0 if k == 0 else _beta_quantile(alpha / 2.0, k, n - k + 1)
    high = 1.0 if k == n else _beta_quantile(1.0 - alpha / 2.0, k + 1, n - k)
    return (low, high)


Observation = tuple[str, int, float]


def design_effect(weights: Sequence[float]) -> dict[str, Any]:
    """Kish: an unequal-probability sample of n rows carries the information of `n_effective`."""
    total = sum(weights)
    squares = sum(weight * weight for weight in weights)
    effective = (total * total / squares) if squares > 0 else 0.0
    return {
        "n": len(weights),
        "n_effective": effective,
        "design_effect": (len(weights) / effective) if effective > 0 else None,
        "weight_min": min(weights) if weights else None,
        "weight_max": max(weights) if weights else None,
    }


def hajek_rate(observations: Sequence[Observation]) -> float | None:
    """Sum(w*y) / sum(w) — the ratio estimator every cohort-level rate here is built on."""
    total = sum(weight for _, _, weight in observations)
    if total <= 0:
        return None
    return sum(weight * y for _, y, weight in observations) / total


def stratified_bootstrap_ci(
    observations: Sequence[Observation],
    *,
    draws: int = BOOTSTRAP_DRAWS,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = ALPHA_95,
) -> dict[str, Any]:
    """Percentile interval on the Hajek rate, resampling WITHIN stratum (§9 metric 2).

    A stratified draw's uncertainty is not binomial: the weights are part of the estimator, so
    the resample has to reproduce the design. Strata are resampled independently at their own
    realised size; a stratum of one contributes no variance and is reported as such."""
    if not observations:
        return {"estimate": None, "lb": None, "ub": None, "draws": 0, "n": 0, "n_strata": 0}
    grouped: dict[str, list[tuple[int, float]]] = {}
    for stratum, y, weight in observations:
        grouped.setdefault(stratum, []).append((y, weight))
    point = hajek_rate(observations)
    rng = random.Random(seed)
    estimates: list[float] = []
    members = [list(rows) for rows in grouped.values()]
    for _ in range(draws):
        numerator = denominator = 0.0
        for rows in members:
            for y, weight in rng.choices(rows, k=len(rows)):
                numerator += weight * y
                denominator += weight
        if denominator > 0:
            estimates.append(numerator / denominator)
    estimates.sort()
    if not estimates:
        return {"estimate": point, "lb": None, "ub": None, "draws": 0,
                "n": len(observations), "n_strata": len(grouped)}
    low = estimates[max(0, int(math.floor((alpha / 2.0) * len(estimates))))]
    high = estimates[min(len(estimates) - 1, int(math.ceil((1.0 - alpha / 2.0) * len(estimates))))]
    singletons = sum(1 for rows in members if len(rows) < 2)
    return {
        "estimate": point,
        "lb": low,
        "ub": high,
        "draws": len(estimates),
        "n": len(observations),
        "n_strata": len(grouped),
        "n_singleton_strata": singletons,
        **design_effect([weight for _, _, weight in observations]),
    }


# --- counting ------------------------------------------------------------------------------


@dataclass(slots=True)
class Cell:
    """One denominator, everything that had to be kept out of it, and the design behind it."""

    n_pairs: int = 0
    n_judged: int = 0
    n_positive: int = 0
    n_abstain: int = 0
    n_unlabelled: int = 0
    n_must_not_link: int = 0
    weight_judged: float = 0.0
    weight_positive: float = 0.0
    weight_abstain: float = 0.0
    observations: list[Observation] = field(default_factory=list)

    def add(self, label: Label | None, weight: float = 1.0, stratum: str = "(none)") -> None:
        self.n_pairs += 1
        if label is None:
            self.n_unlabelled += 1
            return
        if label.must_not_link:
            self.n_must_not_link += 1
        if label.y is None:
            self.n_abstain += 1
            self.weight_abstain += weight
            return
        self.n_judged += 1
        self.weight_judged += weight
        self.observations.append((stratum, int(label.y), weight))
        if label.y == 1:
            self.n_positive += 1
            self.weight_positive += weight

    def absorb(self, other: "Cell") -> None:
        self.n_pairs += other.n_pairs
        self.n_judged += other.n_judged
        self.n_positive += other.n_positive
        self.n_abstain += other.n_abstain
        self.n_unlabelled += other.n_unlabelled
        self.n_must_not_link += other.n_must_not_link
        self.weight_judged += other.weight_judged
        self.weight_positive += other.weight_positive
        self.weight_abstain += other.weight_abstain
        self.observations.extend(other.observations)

    @property
    def rate(self) -> float | None:
        return (self.n_positive / self.n_judged) if self.n_judged else None

    @property
    def weighted_rate(self) -> float | None:
        return (self.weight_positive / self.weight_judged) if self.weight_judged > 0 else None

    def to_json(self, *, bootstrap: bool = False, draws: int = BOOTSTRAP_DRAWS) -> dict[str, Any]:
        wilson = wilson_interval(self.n_positive, self.n_judged) if self.n_judged else (None, None)
        exact = clopper_pearson(self.n_positive, self.n_judged) if self.n_judged else (None, None)
        body: dict[str, Any] = {
            "n_pairs": self.n_pairs,
            "n_judged": self.n_judged,
            "n_positive": self.n_positive,
            "n_abstain": self.n_abstain,
            "n_unlabelled": self.n_unlabelled,
            "n_must_not_link": self.n_must_not_link,
            "rate": self.rate,
            "wilson_lb": wilson[0],
            "wilson_ub": wilson[1],
            "clopper_pearson_lb": exact[0],
            "clopper_pearson_ub": exact[1],
            "weight_judged": self.weight_judged,
            "weight_positive": self.weight_positive,
            "weight_abstain": self.weight_abstain,
            "weighted_rate": self.weighted_rate,
        }
        if bootstrap:
            body["bootstrap"] = stratified_bootstrap_ci(self.observations, draws=draws)
        return body


def _table(
    cells: Mapping[str, Cell], *, bootstrap: bool = False, draws: int = BOOTSTRAP_DRAWS
) -> dict[str, Any]:
    return {
        key: cells[key].to_json(bootstrap=bootstrap, draws=draws) for key in sorted(cells)
    }


def certificate_class(row: Mapping[str, Any]) -> str:
    certificate = row.get("certificate")
    return str(certificate) if certificate else "model"


def _side(row: Mapping[str, Any]) -> str:
    return "cross" if row.get("cross_source") else "same"


def _score(row: Mapping[str, Any]) -> float:
    return float(row.get("score") or 0.0)


def _feats(row: Mapping[str, Any]) -> dict[str, tuple[float, bool]]:
    out: dict[str, tuple[float, bool]] = {}
    for name, entry in (row.get("feats") or {}).items():
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            out[str(name)] = (float(entry[0]), bool(entry[1]))
    return out


def decide_context(row: Mapping[str, Any]) -> dict[str, Any]:
    """What `decide_pair` knew about this pair, read back off the stored row (E11/E22/E24).

    `blocked` is the layer the score cannot reach in either direction — a guard veto or an
    auto-reject; `diverse` is the E11 evidence gate that demotes a pair to the band however high
    it scores; a certificate merges at any score at all. A threshold search that ignores the
    three is describing a decision rule the engine does not run."""
    reason = str(row.get("reason") or "")
    return {
        "certificate": (str(row["certificate"]) if row.get("certificate") else None),
        "diverse": len(row.get("families") or ()) >= MIN_EVIDENCE_FAMILIES,
        "blocked": bool(row.get("veto")) or reason.startswith("auto_reject"),
    }


# --- thresholds ----------------------------------------------------------------------------


@dataclass(slots=True)
class ScoredRow:
    """One judged pair as the threshold search sees it: a score, a class, the DESIGN weight
    (never tier credibility — that belongs to the fit), and what the zone rule would do to it."""

    score: float
    y: int
    weight: float = 1.0
    stratum: str = "(none)"
    certificate: str | None = None
    diverse: bool = True
    blocked: bool = False
    split: str = "train"

    @property
    def merged_regardless(self) -> bool:
        return (not self.blocked) and self.diverse and self.certificate is not None

    @property
    def score_governed(self) -> bool:
        """The pairs a score cut actually decides: no certificate, not blocked, diverse."""
        return (not self.blocked) and self.diverse and self.certificate is None

    def merged_at(self, t: float, *, zone_rule: bool) -> bool:
        if not zone_rule:
            return self.score >= t
        if self.blocked or not self.diverse:
            return False
        return self.certificate is not None or self.score >= t


def _normalise_scored(rows: Iterable[Any]) -> list[ScoredRow]:
    """Accept `ScoredRow`s or bare `(score, y[, weight[, stratum[, context]]])` tuples.

    A bare tuple carries no zone context, so it reads as an ordinary score cut — which is what a
    toy sample means and what a caller testing the arithmetic alone expects."""
    out: list[ScoredRow] = []
    for row in rows:
        if isinstance(row, ScoredRow):
            out.append(row)
            continue
        items = tuple(row)
        scored = ScoredRow(score=float(items[0]), y=int(items[1]))
        if len(items) > 2:
            scored.weight = float(items[2])
        if len(items) > 3:
            scored.stratum = str(items[3])
        if len(items) > 4 and isinstance(items[4], Mapping):
            context = items[4]
            scored.certificate = (
                str(context["certificate"]) if context.get("certificate") else None
            )
            scored.diverse = bool(context.get("diverse", True))
            scored.blocked = bool(context.get("blocked", False))
        out.append(scored)
    return out


def _gates(k: int, n: int, precision_lb: float, precision_point: float) -> bool:
    return bool(n) and k / n >= precision_point and wilson_lower(k, n) >= precision_lb


def _t_hi_search(
    rows: Sequence[ScoredRow],
    precision_lb: float,
    precision_point: float,
    *,
    zone_rule: bool = True,
) -> tuple[float | None, int, int]:
    """Smallest cut whose SIMULATED merge set clears both gates; returns (t, k, n) there.

    Simulated, not `score >= t`: certificate pairs sit in the merge set at every cut and blocked
    or single-family pairs sit outside it at every cut, so the cut only ever moves the
    score-governed pairs in and out. Precision is not monotone in t, so every distinct governed
    score is tried and the smallest winner kept."""
    if zone_rule:
        fixed = [row for row in rows if row.merged_regardless]
        movable = [row for row in rows if row.score_governed]
    else:
        fixed = []
        movable = list(rows)
    k = sum(row.y for row in fixed)
    n = len(fixed)
    best: tuple[float | None, int, int] = (1.0, k, n) if _gates(
        k, n, precision_lb, precision_point
    ) else (None, 0, 0)
    ordered = sorted(movable, key=lambda row: -row.score)
    index = 0
    while index < len(ordered):
        score = ordered[index].score
        while index < len(ordered) and ordered[index].score == score:
            k += ordered[index].y
            n += 1
            index += 1
        if _gates(k, n, precision_lb, precision_point):
            best = (score, k, n)
    return best


def merge_set(rows: Sequence[ScoredRow], t: float, *, zone_rule: bool = True) -> list[ScoredRow]:
    return [row for row in rows if row.merged_at(t, zone_rule=zone_rule)]


def _per_stratum_precision(rows: Sequence[ScoredRow]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[ScoredRow]] = {}
    for row in rows:
        grouped.setdefault(row.stratum, []).append(row)
    out: dict[str, dict[str, Any]] = {}
    for name in sorted(grouped):
        members = grouped[name]
        k, n = sum(row.y for row in members), len(members)
        out[name] = {
            "n": n,
            "n_positive": k,
            "precision": (k / n) if n else None,
            "wilson_lb": wilson_lower(k, n) if n else None,
        }
    return out


def measure_thresholds(
    rows: Iterable[Any],
    t_hi: float | None,
    t_lo: float,
    *,
    zone_rule: bool = True,
    stratum_floor: float = STRATUM_FLOOR,
    draws: int = BOOTSTRAP_DRAWS,
) -> dict[str, Any]:
    """Score a FIXED (T_hi, T_lo) over rows that had no say in choosing it — §6's sealed split.

    The same arithmetic as the search, run once: the point is that the rows are different."""
    scored = _normalise_scored(rows)
    if t_hi is None or not scored:
        return {"n": len(scored), "t_hi": t_hi, "t_lo": t_lo, "precision": None,
                "n_merge": 0, "n_positive": 0}
    merged = merge_set(scored, t_hi, zone_rule=zone_rule)
    k, n = sum(row.y for row in merged), len(merged)
    per_stratum = _per_stratum_precision(merged)
    floors = [body["precision"] for body in per_stratum.values() if body["precision"] is not None]
    weight_positive = sum(row.weight for row in scored if row.y == 1)
    missed = sum(
        row.weight for row in scored
        if row.y == 1 and not row.merged_at(t_hi, zone_rule=zone_rule)
        and (row.score <= t_lo or (zone_rule and row.blocked))
    )
    return {
        "n": len(scored),
        "t_hi": t_hi,
        "t_lo": t_lo,
        "n_merge": n,
        "n_positive": k,
        "precision": (k / n) if n else None,
        "wilson_lb": wilson_lower(k, n) if n else None,
        "clopper_pearson_lb": clopper_pearson(k, n)[0] if n else None,
        "clopper_pearson_ub": clopper_pearson(k, n)[1] if n else None,
        "weighted_precision": hajek_rate(
            [(row.stratum, row.y, row.weight) for row in merged]
        ),
        "bootstrap": stratified_bootstrap_ci(
            [(row.stratum, row.y, row.weight) for row in merged], draws=draws
        ),
        "per_stratum_precision": per_stratum,
        "min_stratum_precision": min(floors) if floors else None,
        "stratum_floor": stratum_floor,
        "stratum_floor_ok": (min(floors) >= stratum_floor) if floors else None,
        "missed_share_at_t_lo": (missed / weight_positive) if weight_positive > 0 else None,
    }


@dataclass(slots=True)
class ThresholdReport:
    t_hi: float | None
    t_lo: float
    band_width: float | None
    n: int
    n_positive: int
    weight_positive: float
    precision_at_t_hi: float | None
    precision_lb_at_t_hi: float | None
    n_at_t_hi: int
    missed_share_at_t_lo: float
    per_stratum_t_hi: dict[str, float | None] = field(default_factory=dict)
    criteria: dict[str, float] = field(default_factory=dict)
    t_hi_score_cut_only: float | None = None
    weighted_precision_at_t_hi: float | None = None
    weighted_precision_gate_ok: bool | None = None
    bootstrap_at_t_hi: dict[str, Any] = field(default_factory=dict)
    clopper_pearson_lb_at_t_hi: float | None = None
    n_certificate_in_merge_set: int = 0
    per_stratum_precision_at_t_hi: dict[str, Any] = field(default_factory=dict)
    min_stratum_precision: float | None = None
    stratum_floor_ok: bool | None = None
    t_lo_raw: float = 0.0
    t_lo_clamped_to: str | None = None
    weight_positive_unavoidable: float = 0.0
    band_width_source: str = "none"
    band_width_weighted_sample: float | None = None
    band_width_population: float | None = None
    band_denominator_n: int = 0
    settings_loadable: bool = False
    settings_reason: str | None = None
    zone_rule: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "t_hi": self.t_hi,
            "t_lo": self.t_lo,
            "band_width": self.band_width,
            "n": self.n,
            "n_positive": self.n_positive,
            "weight_positive": self.weight_positive,
            "precision_at_t_hi": self.precision_at_t_hi,
            "precision_lb_at_t_hi": self.precision_lb_at_t_hi,
            "clopper_pearson_lb_at_t_hi": self.clopper_pearson_lb_at_t_hi,
            "weighted_precision_at_t_hi": self.weighted_precision_at_t_hi,
            "weighted_precision_gate_ok": self.weighted_precision_gate_ok,
            "bootstrap_at_t_hi": self.bootstrap_at_t_hi,
            "n_at_t_hi": self.n_at_t_hi,
            "n_certificate_in_merge_set": self.n_certificate_in_merge_set,
            "t_hi_score_cut_only": self.t_hi_score_cut_only,
            "missed_share_at_t_lo": self.missed_share_at_t_lo,
            "t_lo_raw": self.t_lo_raw,
            "t_lo_clamped_to": self.t_lo_clamped_to,
            "weight_positive_unavoidable": self.weight_positive_unavoidable,
            "per_stratum_t_hi": dict(sorted(self.per_stratum_t_hi.items())),
            "per_stratum_precision_at_t_hi": self.per_stratum_precision_at_t_hi,
            "min_stratum_precision": self.min_stratum_precision,
            "stratum_floor_ok": self.stratum_floor_ok,
            "band_width_source": self.band_width_source,
            "band_width_weighted_sample": self.band_width_weighted_sample,
            "band_width_population": self.band_width_population,
            "band_denominator_n": self.band_denominator_n,
            "settings_loadable": self.settings_loadable,
            "settings_reason": self.settings_reason,
            "zone_rule": self.zone_rule,
            "criteria": dict(self.criteria),
        }


def thresholds(
    scores_with_labels_and_weights: Iterable[Any],
    *,
    precision_lb: float = 0.99,
    precision_point: float = 0.995,
    max_missed: float = 0.03,
    stratum_floor: float = STRATUM_FLOOR,
    store_floor: float = 0.0,
    population_scores: Sequence[float] | None = None,
    zone_rule: bool = True,
    draws: int = BOOTSTRAP_DRAWS,
) -> ThresholdReport:
    """T_hi from the judged tail under the zone rule (D3), T_lo from the HT-weighted positives
    below it (E25), and the band between them as the budget dial.

    The weights here are DESIGN weights: `w = n_total / n_selected`. Multiplying in a tier
    credibility would deflate exactly the low-score positives the budget is meant to protect
    (text judges the band, gold the merge zone), so credibility stays in `fit_model`.

    `t_hi=None` means unattainable on this sample — the honest answer, never a relaxed gate."""
    rows = _normalise_scored(scores_with_labels_and_weights)
    t_hi, k_hi, n_hi = _t_hi_search(rows, precision_lb, precision_point, zone_rule=zone_rule)
    t_hi_cut_only = _t_hi_search(
        [row for row in rows if row.score_governed] if zone_rule else rows,
        precision_lb, precision_point, zone_rule=False,
    )[0]

    positives = [row for row in rows if row.y == 1]
    total_weight = sum(row.weight for row in positives)
    budget = max_missed * total_weight

    # A positive an auto-reject or a guard already threw away is missed whatever T_lo is, so it
    # is spent against the budget FIRST rather than quietly left out of the recall claim. A
    # non-diverse positive is NOT unavoidable: E11 sends it to the band, where the judge sees it.
    unavoidable = sum(row.weight for row in positives if zone_rule and row.blocked)
    governed = sorted(
        (row for row in positives
         if not (zone_rule and (row.blocked or row.merged_regardless))),
        key=lambda row: row.score,
    )
    t_lo = 0.0
    accumulated = unavoidable
    index = 0
    while index < len(governed):
        score = governed[index].score
        if t_hi is not None and score >= t_hi:
            break
        block = 0.0
        probe = index
        while probe < len(governed) and governed[probe].score == score:
            block += governed[probe].weight
            probe += 1
        # The cut rejects the WHOLE tie group (`score <= t_lo`), so the whole group is tested
        # against the budget — stopping mid-group reports 3% while sacrificing the rest unseen.
        if accumulated + block > budget:
            break
        accumulated += block
        t_lo = score
        index = probe

    t_lo_raw = t_lo
    clamped: str | None = None
    if t_hi is not None and t_lo > t_hi:
        t_lo, clamped = t_hi, "t_hi"
    if store_floor > t_lo:
        t_lo, clamped = store_floor, "store_floor"
    if clamped is not None:
        accumulated = unavoidable + sum(
            row.weight for row in governed if row.score <= t_lo
        )

    per_stratum: dict[str, float | None] = {}
    grouped: dict[str, list[ScoredRow]] = {}
    for row in rows:
        grouped.setdefault(row.stratum, []).append(row)
    for name, members in grouped.items():
        per_stratum[name] = _t_hi_search(
            members, precision_lb, precision_point, zone_rule=zone_rule
        )[0]

    merged = merge_set(rows, t_hi, zone_rule=zone_rule) if t_hi is not None else []
    observations = [(row.stratum, row.y, row.weight) for row in merged]
    # D3's gate is a Wilson LB on the JUDGED COUNTS, so that is what `t_hi` is chosen on. The
    # design-weighted rate at the same cut rides alongside: when the two disagree the sample is
    # telling you the cut holds on the pairs drawn, not on the cohort they stand for.
    weighted_point = hajek_rate(observations)
    per_stratum_precision = _per_stratum_precision(merged)
    floors = [
        body["precision"] for body in per_stratum_precision.values()
        if body["precision"] is not None
    ]

    band_population: float | None = None
    band_sample: float | None = None
    denominator = 0
    if t_hi is not None:
        if population_scores:
            in_band = sum(1 for score in population_scores if t_lo < score < t_hi)
            band_population = in_band / len(population_scores)
            denominator = len(population_scores)
        weight_total = sum(row.weight for row in rows)
        if weight_total > 0:
            band_sample = sum(
                row.weight for row in rows if t_lo < row.score < t_hi
            ) / weight_total
    band_width = band_population if band_population is not None else band_sample
    source = "population" if band_population is not None else (
        "weighted_sample" if band_sample is not None else "none"
    )

    loadable = t_hi is not None and 0.0 <= store_floor <= t_lo <= t_hi <= 1.0
    reason = None if loadable else (
        "t_hi unattainable" if t_hi is None else "store_floor > t_lo or t_lo > t_hi"
    )

    return ThresholdReport(
        t_hi=t_hi,
        t_lo=t_lo,
        band_width=band_width,
        n=len(rows),
        n_positive=len(positives),
        weight_positive=total_weight,
        precision_at_t_hi=(k_hi / n_hi) if n_hi else None,
        precision_lb_at_t_hi=wilson_lower(k_hi, n_hi) if n_hi else None,
        clopper_pearson_lb_at_t_hi=clopper_pearson(k_hi, n_hi)[0] if n_hi else None,
        weighted_precision_at_t_hi=weighted_point,
        weighted_precision_gate_ok=(
            None if weighted_point is None else weighted_point >= precision_point
        ),
        bootstrap_at_t_hi=(
            stratified_bootstrap_ci(observations, draws=draws) if observations else {}
        ),
        n_at_t_hi=n_hi,
        n_certificate_in_merge_set=sum(1 for row in merged if row.certificate),
        t_hi_score_cut_only=t_hi_cut_only,
        missed_share_at_t_lo=(accumulated / total_weight) if total_weight > 0 else 0.0,
        t_lo_raw=t_lo_raw,
        t_lo_clamped_to=clamped,
        weight_positive_unavoidable=unavoidable,
        per_stratum_t_hi=per_stratum,
        per_stratum_precision_at_t_hi=per_stratum_precision,
        min_stratum_precision=min(floors) if floors else None,
        stratum_floor_ok=(min(floors) >= stratum_floor) if floors else None,
        band_width_source=source,
        band_width_weighted_sample=band_sample,
        band_width_population=band_population,
        band_denominator_n=denominator,
        settings_loadable=loadable,
        settings_reason=reason,
        zone_rule=zone_rule,
        criteria={
            "precision_lb": precision_lb,
            "precision_point": precision_point,
            "max_missed": max_missed,
            "stratum_floor": stratum_floor,
            "store_floor": store_floor,
        },
    )


# --- the evaluation ------------------------------------------------------------------------


@dataclass(slots=True)
class EvalReport:
    sections: dict[str, Any] = field(default_factory=dict)
    title: str = "autodedup evaluation"

    def to_json(self) -> dict[str, Any]:
        return {"title": self.title, **self.sections}

    def headline(self) -> list[str]:
        return _eval_headline(self)

    def to_markdown(self) -> str:
        return _eval_markdown(self)


def _pct(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value * 100:.{digits}f}%"


def evaluate(
    run_pairs: Sequence[Mapping[str, Any]],
    labels: Mapping[PairKey, Label],
    sample: Sample | None,
    settings: Settings,
    *,
    by_tier: Mapping[str, Mapping[PairKey, Label]] | None = None,
    precedence: Sequence[str] | None = None,
    seed: int = SPLIT_SEED,
    draws: int = BOOTSTRAP_DRAWS,
) -> EvalReport:
    """Every §9 number this program can compute from one run plus one set of judgements."""
    draw = sample if sample is not None else EMPTY_SAMPLE
    weighted = draw.is_weighted

    zones: dict[str, Cell] = {}
    merge_by_stratum: dict[str, Cell] = {}
    merge_by_certificate: dict[str, Cell] = {}
    band_by_certificate: dict[str, Cell] = {}
    reject_by_stratum: dict[str, Cell] = {}
    agreement_block: dict[str, list[int]] = {}
    agreement_source: dict[str, list[int]] = {}
    agreement_side: dict[str, list[int]] = {}
    response: dict[str, list[float]] = {}
    scored: list[ScoredRow] = []

    groups = split_groups(run_pairs, labels)
    n_rows = 0
    n_labelled = 0
    n_defaulted_weight = 0
    weight_positive_total = 0.0
    weight_positive_below_t_lo = 0.0
    weight_positive_in_band = 0.0
    developer_suspected = 0

    seen: set[PairKey] = set()
    for row in run_pairs:
        key = pair_key(row["lo"], row["hi"])
        seen.add(key)
        n_rows += 1
        zone = str(row.get("zone") or "?")
        label = labels.get(key)
        # The judgement row carries the stratum the SAMPLER stamped on it; recomputing one is a
        # second opinion about the design and is only the fallback (see labels.load_sample).
        stratum = (label.stratum if label and label.stratum else draw.stratum_of(key)) or "(none)"
        weight = draw.weight_for_name(stratum) if (
            label is not None and label.stratum
        ) else draw.weight_of(key)
        zones.setdefault(zone, Cell()).add(label, weight, stratum)
        if label is not None:
            n_labelled += 1
            if label.developer_project_suspected:
                developer_suspected += 1
            if label.y is not None and not draw.inflates(stratum):
                n_defaulted_weight += 1
            entry = response.setdefault(stratum, [0.0, 0.0, 0.0, 0.0])
            entry[0] += 1
            entry[2] += weight
            if label.y is not None:
                entry[1] += 1
                entry[3] += weight
        if zone == MERGE_ZONE:
            merge_by_stratum.setdefault(stratum, Cell()).add(label, weight, stratum)
            merge_by_certificate.setdefault(certificate_class(row), Cell()).add(
                label, weight, stratum
            )
        elif zone == BAND_ZONE:
            band_by_certificate.setdefault(certificate_class(row), Cell()).add(
                label, weight, stratum
            )
        elif zone == REJECT_ZONE:
            reject_by_stratum.setdefault(stratum, Cell()).add(label, weight, stratum)

        if label is None or label.y is None:
            continue
        context = decide_context(row)
        score = _score(row)
        scored.append(ScoredRow(
            score=score,
            y=int(label.y),
            weight=weight,
            stratum=stratum,
            certificate=context["certificate"],
            diverse=bool(context["diverse"]),
            blocked=bool(context["blocked"]),
            split=split_of(group_of(key, groups), seed),
        ))
        if label.y == 1:
            weight_positive_total += weight
            if score <= settings.t_lo:
                weight_positive_below_t_lo += weight
            elif score < settings.t_hi:
                weight_positive_in_band += weight
        if zone in DECIDED_ZONES:
            agreed = 1 if (zone == MERGE_ZONE) == (label.y == 1) else 0
            for table, name in (
                (agreement_block, str(row.get("block") or "(none)")),
                (agreement_source, str(row.get("source_pair") or "?")),
                (agreement_side, _side(row)),
            ):
                counter = table.setdefault(name, [0, 0])
                counter[0] += agreed
                counter[1] += 1

    merge_pooled = Cell()
    for cell in merge_by_certificate.values():
        merge_pooled.absorb(cell)

    population = [_score(row) for row in run_pairs]
    dev = [row for row in scored if row.split in DEV_SPLITS]
    sealed = [row for row in scored if row.split == "test"]
    criteria = {"population_scores": population, "store_floor": settings.store_floor,
                "draws": draws}
    threshold_report = thresholds(dev or scored, **criteria)
    threshold_all = thresholds(scored, **criteria)
    holdout = measure_thresholds(
        sealed, threshold_report.t_hi, threshold_report.t_lo, draws=draws
    )

    tiers = by_tier or {}
    tier_rows: dict[str, Any] = {}
    for tier, mapping in sorted(tiers.items()):
        total = len(mapping)
        abstain = sum(1 for label in mapping.values() if label.y is None)
        positive = sum(1 for label in mapping.values() if label.y == 1)
        tier_rows[tier] = {
            "n": total,
            "n_positive": positive,
            "n_insufficient_evidence": abstain,
            "insufficient_evidence_rate": (abstain / total) if total else None,
        }

    report = EvalReport(
        sections={
            "counts": {
                "run_pairs": n_rows,
                "labelled_pairs": n_labelled,
                "labels_off_run": sorted(set(labels) - seen)[:20],
                "n_labels_off_run": len(set(labels) - seen),
                "developer_project_suspected": developer_suspected,
                "weighted": weighted,
                "n_labelled_without_sample_weight": n_defaulted_weight,
            },
            "precedence": list(precedence) if precedence else None,
            "settings": {"t_hi": settings.t_hi, "t_lo": settings.t_lo,
                         "store_floor": settings.store_floor},
            "sample": {
                **draw.to_json(),
                "nonresponse": _nonresponse(response),
                "design": design_effect([row.weight for row in scored]),
            },
            "zones": _table(zones, bootstrap=True, draws=draws),
            "merge_precision": {
                "pooled": merge_pooled.to_json(bootstrap=True, draws=draws),
                "by_certificate": _table(merge_by_certificate, bootstrap=True, draws=draws),
                "by_stratum": _table(merge_by_stratum),
            },
            "band_composition": {
                "pooled": zones.get(BAND_ZONE, Cell()).to_json(bootstrap=True, draws=draws),
                "by_certificate": _table(band_by_certificate),
                # E25's dial at the thresholds ACTUALLY in force, read off the stored population
                # rather than the band-enriched judge sample. Pairs below `store_floor` are not
                # stored and so are not in this denominator — they are below T_lo by definition.
                "population_share_at_settings": (
                    sum(1 for score in population if settings.t_lo < score < settings.t_hi)
                    / len(population)
                ) if population else None,
                "weighted_sample_share_at_settings": (
                    sum(row.weight for row in scored
                        if settings.t_lo < row.score < settings.t_hi)
                    / sum(row.weight for row in scored)
                ) if scored else None,
                "n_population": len(population),
                "store_floor": settings.store_floor,
            },
            "reject_zone": {
                "pooled": zones.get(REJECT_ZONE, Cell()).to_json(bootstrap=True, draws=draws),
                "by_stratum": _table(reject_by_stratum),
            },
            "agreement": {
                "by_block": _agreement(agreement_block),
                "by_source_pair": _agreement(agreement_source),
                "by_side": _agreement(agreement_side),
            },
            "cohort": {
                "weighted": weighted,
                "merge_precision_ht": merge_pooled.weighted_rate,
                "merge_precision_bootstrap": stratified_bootstrap_ci(
                    merge_pooled.observations, draws=draws
                ),
                "merge_precision_unweighted": merge_pooled.rate,
                "weight_positive_total": weight_positive_total,
                "weight_positive_below_t_lo": weight_positive_below_t_lo,
                "positives_below_t_lo_share": (
                    weight_positive_below_t_lo / weight_positive_total
                    if weight_positive_total > 0 else None
                ),
                "weight_positive_in_band": weight_positive_in_band,
                "positives_in_band_share": (
                    weight_positive_in_band / weight_positive_total
                    if weight_positive_total > 0 else None
                ),
            },
            "tiers": tier_rows,
            "fidelity": judge_fidelity(tiers),
            "thresholds": threshold_report.to_json(),
            "thresholds_all_judged": threshold_all.to_json(),
            "holdout": {
                "split_seed": seed,
                "n_dev": len(dev),
                "n_sealed": len(sealed),
                "fitted_on": "train+validation" if dev else "all judged (no dev rows)",
                **holdout,
            },
        }
    )
    return report


def _nonresponse(response: Mapping[str, Sequence[float]]) -> dict[str, Any]:
    """What the abstentions took out of the estimator (§9's unstated MAR assumption, named).

    An `insufficient_evidence` pair leaves the denominator, and its inclusion weight leaves with
    it. The ratios published here survive nonresponse that is uniform WITHIN a stratum; they do
    not survive nonresponse that correlates with the verdict, so the shortfall is reported per
    stratum rather than assumed away."""
    weight_all = sum(body[2] for body in response.values())
    weight_decided = sum(body[3] for body in response.values())
    short = sorted(
        (
            {
                "stratum": name,
                "n_labelled": int(body[0]),
                "n_decided": int(body[1]),
                "response_rate": (body[1] / body[0]) if body[0] else None,
                "weight_lost": body[2] - body[3],
            }
            for name, body in response.items()
            if body[1] < body[0]
        ),
        key=lambda entry: -float(entry["weight_lost"] or 0.0),
    )
    return {
        "n_strata": len(response),
        "n_strata_with_abstentions": len(short),
        "weight_labelled": weight_all,
        "weight_decided": weight_decided,
        "weight_lost_to_abstention": weight_all - weight_decided,
        "weight_lost_share": (
            (weight_all - weight_decided) / weight_all if weight_all > 0 else None
        ),
        "worst_strata": short[:10],
    }


def _agreement(table: Mapping[str, Sequence[int]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in sorted(table):
        agreed, total = table[key][0], table[key][1]
        out[key] = {
            "n": total,
            "n_agree": agreed,
            "agreement": (agreed / total) if total else None,
            "wilson_lb": wilson_interval(agreed, total)[0] if total else None,
        }
    return out


def judge_fidelity(
    by_tier: Mapping[str, Mapping[PairKey, Label]],
    *,
    gold_tier: str = "gold",
    cheap_tiers: Sequence[str] = CHEAP_TIERS,
) -> dict[str, Any]:
    """Metric 8's cheap-vs-gold number, over the pairs BOTH tiers actually decided.

    Agreement is on the two-class collapse, and a pair either tier abstained on is excluded:
    an abstention is not a disagreement, and counting it as one would make the cheap tier look
    worse exactly where it correctly refused to guess."""
    gold = by_tier.get(gold_tier) or {}
    out: dict[str, Any] = {"gold_tier": gold_tier, "by_cheap_tier": {}}
    pooled_agree = pooled_total = 0
    for tier in cheap_tiers:
        cheap = by_tier.get(tier) or {}
        shared = sorted(set(gold) & set(cheap))
        both = [
            key for key in shared
            if gold[key].y is not None and cheap[key].y is not None
        ]
        agree = sum(1 for key in both if gold[key].y == cheap[key].y)
        verdict_agree = sum(1 for key in both if gold[key].verdict == cheap[key].verdict)
        pooled_agree += agree
        pooled_total += len(both)
        out["by_cheap_tier"][tier] = {
            "n_shared": len(shared),
            "n_both_decided": len(both),
            "n_agree": agree,
            "agreement": (agree / len(both)) if both else None,
            "wilson_lb": wilson_interval(agree, len(both))[0] if both else None,
            "verdict_agreement": (verdict_agree / len(both)) if both else None,
        }
    out["pooled"] = {
        "n": pooled_total,
        "n_agree": pooled_agree,
        "agreement": (pooled_agree / pooled_total) if pooled_total else None,
        "wilson_lb": wilson_interval(pooled_agree, pooled_total)[0] if pooled_total else None,
    }
    gold_unanimous = [label for label in gold.values() if label.unanimous is not None]
    out["gold_unanimity"] = (
        sum(1 for label in gold_unanimous if label.unanimous) / len(gold_unanimous)
        if gold_unanimous else None
    )
    return out


# --- the fit -------------------------------------------------------------------------------


class _Union:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, item: int) -> int:
        parent = self.parent
        if item not in parent:
            parent[item] = item
            return item
        root = parent[item]
        while root != parent[root]:
            parent[root] = parent[parent[root]]
            root = parent[root]
        parent[item] = root
        return root

    def union(self, left: int, right: int) -> int:
        a, b = self.find(left), self.find(right)
        if a == b:
            return a
        low, high = (a, b) if a < b else (b, a)
        self.parent[high] = low
        return low


def merge_groups(run_pairs: Sequence[Mapping[str, Any]]) -> dict[int, int]:
    """Connected components of the merge edges; the root IS the smallest listing id (E36)."""
    union = _Union()
    for row in run_pairs:
        if row.get("zone") == MERGE_ZONE:
            union.union(int(row["lo"]), int(row["hi"]))
    return {item: union.find(item) for item in union.parent}


def split_groups(
    run_pairs: Sequence[Mapping[str, Any]],
    labels: Mapping[PairKey, Label] | None = None,
) -> dict[int, int]:
    """The map the 60/20/20 split is taken over: merge components, PLUS every labelled pair.

    A merge component alone is not enough. ~60% of the labelled pairs in a real run straddle two
    components (that is what a judged negative usually IS), and pinning such a pair to one of the
    two roots puts a listing's own evidence in two different splits — the leak a cluster split
    exists to prevent. Unioning the labelled pair as well costs nothing (the labelled set is
    small) and makes the split a partition of LISTINGS."""
    union = _Union()
    for row in run_pairs:
        if row.get("zone") == MERGE_ZONE:
            union.union(int(row["lo"]), int(row["hi"]))
    for key, label in (labels or {}).items():
        if label.y is not None:
            union.union(int(key[0]), int(key[1]))
    return {item: union.find(item) for item in union.parent}


def group_of(key: PairKey, groups: Mapping[int, int]) -> int:
    """A pair's split group. Under `split_groups` both ids share a root; the `min` is the
    fallback for a map that does not contain the pair (a singleton, or a stale seal)."""
    lo, hi = key
    return min(groups.get(lo, lo), groups.get(hi, hi))


def split_of(group: int, seed: int = SPLIT_SEED) -> str:
    bucket = int(
        hashlib.blake2b(f"{seed}:{group}".encode("utf-8"), digest_size=8).hexdigest(), 16
    ) % 100
    if bucket < TRAIN_SHARE:
        return "train"
    if bucket < TRAIN_SHARE + VALIDATION_SHARE:
        return "validation"
    return "test"


def split_seal(groups: Mapping[int, int]) -> dict[str, Any]:
    """An identity for the group map, so a refit can PROVE it scored on the incumbent's seal.

    Components are read off `zone == merge` edges, which move when the model or the thresholds
    move — so without a stored seal the "sealed test split" silently re-randomises between a
    challenger and the incumbent, and §9's comparison compares two different holdouts."""
    digest = hashlib.sha256()
    for item in sorted(groups):
        digest.update(f"{item}:{groups[item]}\n".encode("utf-8"))
    return {"sha256": digest.hexdigest(), "n_listings": len(groups),
            "n_groups": len(set(groups.values()))}


@dataclass(slots=True)
class FitReport:
    sections: dict[str, Any] = field(default_factory=dict)
    title: str = "autodedup model fit"

    def to_json(self) -> dict[str, Any]:
        return {"title": self.title, **self.sections}

    def headline(self) -> list[str]:
        return _fit_headline(self)

    def to_markdown(self) -> str:
        return _fit_markdown(self)


def _log_loss(probs: Sequence[float], ys: Sequence[int]) -> float:
    if not probs:
        return 0.0
    total = 0.0
    for prob, y in zip(probs, ys):
        clipped = min(1.0 - 1e-12, max(1e-12, prob))
        total -= math.log(clipped) if y == 1 else math.log(1.0 - clipped)
    return total / len(probs)


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _reliability(
    probs: Sequence[float], ys: Sequence[int], bins: int = RELIABILITY_BINS
) -> list[dict[str, Any]]:
    table: list[dict[str, Any]] = []
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        members = [
            (prob, y) for prob, y in zip(probs, ys)
            if (prob >= low and (prob < high or (index == bins - 1 and prob <= high)))
        ]
        if not members:
            continue
        table.append({
            "bin": f"{low:.1f}-{high:.1f}",
            "n": len(members),
            "mean_predicted": sum(prob for prob, _ in members) / len(members),
            "observed": sum(y for _, y in members) / len(members),
        })
    return table


def fit_model(
    run_pairs: Sequence[Mapping[str, Any]],
    labels: Mapping[PairKey, Label],
    *,
    split: str = "cluster",
    sample: Sample | None = None,
    seed: int = SPLIT_SEED,
    weight_cap: float | None = None,
    weight_cap_multiple: float = WEIGHT_CAP_MEDIAN_MULTIPLE,
    l2: float = 1e-3,
    epochs: int = 3000,
    version: str | None = None,
    split_map: Mapping[int, int] | None = None,
) -> tuple[LogisticModel, FitReport]:
    """Fit the §6 scorer on judged pairs, split 60/20/20 by component (never by pair).

    Training weights ARE label credibility x design weight — a gold row should outvote a text
    row, and a thin stratum stands for more of the cohort. That product is capped at a multiple
    of the median weight rather than at a constant, so the cap tames the tail without re-ordering
    the design, and the realised design effect is reported beside it."""
    if split != "cluster":
        raise ValueError(f"only the cluster split is implemented, got {split!r}")
    draw = sample if sample is not None else EMPTY_SAMPLE
    groups = dict(split_map) if split_map is not None else split_groups(run_pairs, labels)

    staged: list[dict[str, Any]] = []
    n_abstain = n_unlabelled = 0
    for row in run_pairs:
        key = pair_key(row["lo"], row["hi"])
        label = labels.get(key)
        if label is None:
            n_unlabelled += 1
            continue
        if label.y is None:
            n_abstain += 1
            continue
        group = group_of(key, groups)
        staged.append({
            "key": key,
            "group": group,
            "y": int(label.y),
            "weight": label.weight * draw.weight_for(key, label.stratum),
            "feats": _feats(row),
        })

    raw_weights = [row["weight"] for row in staged]
    cap = weight_cap if weight_cap is not None else max(
        1.0, _median(raw_weights) * weight_cap_multiple
    )
    buckets: dict[str, list[dict[str, Any]]] = {"train": [], "validation": [], "test": []}
    assignment: dict[int, str] = {}
    capped = 0
    for row in staged:
        if row["weight"] > cap:
            row["weight"] = cap
            capped += 1
        bucket = assignment.setdefault(row["group"], split_of(row["group"], seed))
        buckets[bucket].append(row)

    train, validation, test = buckets["train"], buckets["validation"], buckets["test"]
    if not train:
        raise ValueError("no training rows: no labelled pair fell in the train split")

    prior = hand_initialised()
    model = hand_initialised()
    model.fit(
        [row["feats"] for row in train],
        [row["y"] for row in train],
        l2=l2,
        epochs=epochs,
        sample_weights=[row["weight"] for row in train],
    )

    pre_ece = None
    if validation:
        raw = [model.predict_proba(row["feats"]) for row in validation]
        pre_ece = model.calibrate(raw, [row["y"] for row in validation])

    def measured(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"n": 0}
        probs = [model.predict_proba(row["feats"]) for row in rows]
        ys = [int(row["y"]) for row in rows]
        prior_probs = [prior.predict_proba(row["feats"]) for row in rows]
        return {
            "n": len(rows),
            "n_positive": sum(ys),
            "n_groups": len({row["group"] for row in rows}),
            "auc": auc(probs, ys),
            "auc_prior": auc(prior_probs, ys),
            "ece": expected_calibration_error(probs, ys),
            "ece_prior": expected_calibration_error(prior_probs, ys),
            "log_loss": _log_loss(probs, ys),
            "log_loss_prior": _log_loss(prior_probs, ys),
        }

    train_metrics = measured(train)
    validation_metrics = measured(validation)
    test_metrics = measured(test)
    model.version = version or f"fit_{len(train)}_{seed}"

    weights = sorted(
        ((name, model.weights.get(name, 0.0)) for name in model.feature_order),
        key=lambda item: -abs(item[1]),
    )
    converged = bool(model.fit_report.get("converged"))
    report = FitReport(
        sections={
            "split": {
                "kind": split,
                "seed": seed,
                "n_groups": len(assignment),
                "train": len(train),
                "validation": len(validation),
                "test": len(test),
                "shares": {"train": TRAIN_SHARE, "validation": VALIDATION_SHARE,
                           "test": 100 - TRAIN_SHARE - VALIDATION_SHARE},
                "n_abstain_skipped": n_abstain,
                "n_unlabelled_skipped": n_unlabelled,
                "n_weights_capped": capped,
                "weight_cap": cap,
                "weight_cap_rule": (
                    "explicit" if weight_cap is not None
                    else f"{weight_cap_multiple:g}x median weight"
                ),
                "weights_before_cap": design_effect(raw_weights) if raw_weights else {},
                "weights_after_cap": design_effect(
                    [row["weight"] for row in staged]
                ) if staged else {},
                "seal": split_seal(groups),
                "from_split_map": split_map is not None,
                "leakage_check": _leakage_check(buckets),
            },
            "train_metrics": train_metrics,
            "validation_metrics": validation_metrics,
            "test_metrics": test_metrics,
            "calibration": {
                # The validation ECE inside `validation_metrics` is measured AFTER the isotonic
                # knots were fitted on that same split: in-sample, and not the E21 gate. The two
                # honest numbers are the pre-fit validation ECE and the sealed test ECE.
                "pre_calibration_ece_validation": pre_ece,
                "post_calibration_ece_validation_in_sample": validation_metrics.get("ece"),
                "test_ece": test_metrics.get("ece"),
                "ece_gate": 0.05,
                "ece_gate_ok": (
                    test_metrics["ece"] <= 0.05 if test_metrics.get("n") else None
                ),
                "knots": [[edge, value] for edge, value in (model.calibration or [])],
            },
            "convergence": {**dict(model.fit_report), "converged_flag": converged,
                            "epochs": epochs, "l2": l2},
            "weights": [{"feature": name, "weight": value} for name, value in weights],
            "presence_weights": [
                {"feature": name, "weight": model.presence_weights.get(name, 0.0)}
                for name, _ in weights
            ],
            "interactions": [
                {"left": left, "right": right, "weight": weight}
                for left, right, weight in model.interactions
            ],
            "reliability_test": (
                _reliability(
                    [model.predict_proba(row["feats"]) for row in test],
                    [int(row["y"]) for row in test],
                ) if test else []
            ),
            "model_version": model.version,
        }
    )
    return model, report


def _leakage_check(buckets: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    """The property that CAN fail: one LISTING must not appear in two splits.

    Checking that a group lives in one split is vacuous — the assignment is a function of the
    group by construction. The real channel is a pair whose two listings sit in different
    components: pin it to one root and the other listing's evidence lands in another split."""
    where: dict[int, set[str]] = {}
    listings: dict[int, set[str]] = {}
    rows_affected = 0
    for name, rows in buckets.items():
        for row in rows:
            where.setdefault(int(row["group"]), set()).add(name)
            for item in row["key"]:
                listings.setdefault(int(item), set()).add(name)
    spanning = sorted(group for group, names in where.items() if len(names) > 1)
    leaked = sorted(item for item, names in listings.items() if len(names) > 1)
    leaked_set = set(leaked)
    for rows in buckets.values():
        for row in rows:
            if leaked_set & {int(item) for item in row["key"]}:
                rows_affected += 1
    return {
        "n_groups": len(where),
        "n_groups_spanning_splits": len(spanning),
        "spanning": spanning[:20],
        "n_listings": len(listings),
        "n_listings_in_multiple_splits": len(leaked),
        "listings_in_multiple_splits": leaked[:20],
        "n_rows_touching_a_leaked_listing": rows_affected,
    }


# --- reports -------------------------------------------------------------------------------


class Report(Protocol):
    title: str

    def to_json(self) -> dict[str, Any]: ...

    def to_markdown(self) -> str: ...


def _row(name: str, cell: Mapping[str, Any]) -> str:
    bootstrap = cell.get("bootstrap") or {}
    return (
        f"| {name} | {cell['n_judged']} | {cell['n_positive']} | "
        f"{_pct(cell['weighted_rate'])} | {_pct(bootstrap.get('lb'))} | "
        f"{_pct(cell['rate'])} | {_pct(cell['wilson_lb'])} | "
        f"{_pct(cell['clopper_pearson_lb'])} | {cell['n_abstain']} | {cell['n_unlabelled']} |"
    )


PRECISION_HEADER: tuple[str, str] = (
    "| cell | judged | positive | HT precision | boot LB | unweighted | wilson LB | CP LB |"
    " abstain | unlabelled |",
    "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
)


def _eval_headline(report: EvalReport) -> list[str]:
    sections = report.sections
    merge = sections["merge_precision"]
    counts = sections["counts"]
    lines: list[str] = []
    marker = "" if counts["weighted"] else "   [UNWEIGHTED: no sample populations]"
    lines.append("merge-zone precision (judge labels vs the engine's auto-merge)" + marker)
    lines.extend(PRECISION_HEADER)
    lines.append(_row("pooled", merge["pooled"]))
    for name in CERTIFICATE_CLASSES:
        cell = merge["by_certificate"].get(name)
        if cell:
            lines.append(_row(name, cell))
    band = sections["band_composition"]["pooled"]
    reject = sections["reject_zone"]["pooled"]
    cohort = sections["cohort"]
    thresholds_json = sections["thresholds"]
    holdout = sections["holdout"]
    lines.append("")
    lines.append(f"band composition      judged {band['n_judged']}  positive share "
                 f"{_pct(band['rate'])}  HT {_pct(band['weighted_rate'])}"
                 f"   b at the settings in force "
                 f"{_pct(sections['band_composition']['population_share_at_settings'])}"
                 f" of {sections['band_composition']['n_population']} stored pairs (E25)")
    lines.append(f"reject zone           judged {reject['n_judged']}  positive share "
                 f"{_pct(reject['rate'])}  HT-weighted {_pct(reject['weighted_rate'])}")
    lines.append(f"missed positives      below T_lo={sections['settings']['t_lo']:.2f}: "
                 f"{_pct(cohort['positives_below_t_lo_share'])} of weighted positives"
                 f"   in band: {_pct(cohort['positives_in_band_share'])}" + marker)
    t_hi = thresholds_json["t_hi"]
    lines.append(
        f"thresholds (dev)      T_hi {'unattainable' if t_hi is None else f'{t_hi:.4f}'}"
        f"  (n {thresholds_json['n_at_t_hi']} of which {thresholds_json['n_certificate_in_merge_set']}"
        f" certificate, precision {_pct(thresholds_json['precision_at_t_hi'])},"
        f" LB {_pct(thresholds_json['precision_lb_at_t_hi'])})"
        f"   T_lo {thresholds_json['t_lo']:.4f}"
        f"   band {_pct(thresholds_json['band_width'])}"
        f" ({thresholds_json['band_width_source']})"
    )
    lines.append(
        f"holdout (sealed)      n {holdout['n_sealed']}  merge set {holdout.get('n_merge', 0)}"
        f"  precision {_pct(holdout.get('precision'))}"
        f"  CP LB {_pct(holdout.get('clopper_pearson_lb'))}"
        f"  min stratum {_pct(holdout.get('min_stratum_precision'))}"
    )
    fidelity = sections["fidelity"]["pooled"]
    lines.append(f"judge fidelity        cheap-vs-gold {_pct(fidelity['agreement'])}"
                 f" on n={fidelity['n']}  (LB {_pct(fidelity['wilson_lb'])})")
    side = sections["agreement"]["by_side"]
    parts = [f"{name} {_pct(body['agreement'])} (n={body['n']})" for name, body in side.items()]
    lines.append(f"judge-vs-engine       {'   '.join(parts) or '-'}")
    nonresponse = sections["sample"]["nonresponse"]
    design = sections["sample"]["design"]
    lines.append(
        f"design                weights {design.get('weight_min')}-{design.get('weight_max')}"
        f"  effective n {design.get('n_effective', 0.0):.1f} of {design.get('n', 0)}"
        f"   abstention lost {_pct(nonresponse['weight_lost_share'])} of cohort weight"
    )
    return lines


def _eval_markdown(report: EvalReport) -> str:
    sections = report.sections
    lines = [f"# {report.title}", ""]
    counts = sections["counts"]
    lines.append(f"- run pairs: **{counts['run_pairs']}**, labelled: **{counts['labelled_pairs']}**"
                 f", labels with no run row: {counts['n_labels_off_run']}")
    lines.append(f"- thresholds in force: T_hi={sections['settings']['t_hi']}, "
                 f"T_lo={sections['settings']['t_lo']}")
    lines.append(f"- Horvitz-Thompson weighting: "
                 f"**{'on' if counts['weighted'] else 'off (sample estimates only)'}**"
                 f", labelled pairs falling back to weight 1.0: "
                 f"{counts['n_labelled_without_sample_weight']}")
    lines.append(f"- tier precedence: {sections.get('precedence') or 'default'}")
    lines.extend(["", "## Headline", "", "```"])
    lines.extend(report.headline())
    lines.extend(["```", "", "## Merge precision by stratum", ""])
    lines.extend(PRECISION_HEADER)
    for name, cell in sections["merge_precision"]["by_stratum"].items():
        lines.append(_row(name, cell))
    lines.extend(["", "## Zones", ""])
    lines.extend(PRECISION_HEADER)
    for name, cell in sections["zones"].items():
        lines.append(_row(name, cell))
    lines.extend(["", "## Judge-vs-engine agreement", "",
                  "| grouping | key | n | agreement | wilson LB |",
                  "| --- | --- | ---: | ---: | ---: |"])
    for grouping in ("by_side", "by_source_pair", "by_block"):
        for key, body in sections["agreement"][grouping].items():
            lines.append(f"| {grouping} | {key} | {body['n']} | {_pct(body['agreement'])} "
                         f"| {_pct(body['wilson_lb'])} |")
    lines.extend(["", "## Tiers", "",
                  "| tier | n | positive | insufficient evidence | rate |",
                  "| --- | ---: | ---: | ---: | ---: |"])
    for tier, body in sections["tiers"].items():
        lines.append(f"| {tier} | {body['n']} | {body['n_positive']} | "
                     f"{body['n_insufficient_evidence']} | "
                     f"{_pct(body['insufficient_evidence_rate'])} |")
    fidelity = sections["fidelity"]
    lines.extend(["", "## Judge fidelity (metric 8)", "",
                  "| cheap tier | both decided | agree | agreement | wilson LB |",
                  "| --- | ---: | ---: | ---: | ---: |"])
    for tier, body in fidelity["by_cheap_tier"].items():
        lines.append(f"| {tier} | {body['n_both_decided']} | {body['n_agree']} | "
                     f"{_pct(body['agreement'])} | {_pct(body['wilson_lb'])} |")
    lines.append("")
    lines.append(f"gold unanimity: {_pct(fidelity['gold_unanimity'])}")
    lines.extend(["", "## Thresholds (fitted on train+validation)", "", "```",
                  json.dumps(sections["thresholds"], indent=2, sort_keys=True), "```", "",
                  "## Thresholds over every judged pair (cross-check, selection-inflated)", "",
                  "```",
                  json.dumps(sections["thresholds_all_judged"], indent=2, sort_keys=True),
                  "```", "",
                  "## Sealed holdout re-measurement", "", "```",
                  json.dumps(sections["holdout"], indent=2, sort_keys=True), "```", "",
                  "## Sample design and nonresponse", "", "```",
                  json.dumps(sections["sample"], indent=2, sort_keys=True), "```", ""])
    return "\n".join(lines)


def _fit_headline(report: FitReport) -> list[str]:
    sections = report.sections
    split = sections["split"]
    check = split["leakage_check"]
    calibration = sections["calibration"]
    lines = [
        f"split (cluster, seed {split['seed']})   groups {split['n_groups']}"
        f"   train {split['train']}  validation {split['validation']}  test {split['test']}"
        f"   leaked listings {check['n_listings_in_multiple_splits']}"
        f"   seal {split['seal']['sha256'][:12]}",
        "",
        "| split | n | positive | AUC | AUC (hand prior) | ECE | log loss | log loss (prior) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name in ("train_metrics", "validation_metrics", "test_metrics"):
        body = sections[name]
        label = name.replace("_metrics", "")
        if name == "validation_metrics":
            label += " (ECE in-sample)"
        if not body.get("n"):
            lines.append(f"| {label} | 0 | - | - | - | - | - | - |")
            continue
        lines.append(
            f"| {label} | {body['n']} | {body['n_positive']} | "
            f"{body['auc']:.4f} | {body['auc_prior']:.4f} | {body['ece']:.4f} | "
            f"{body['log_loss']:.4f} | {body['log_loss_prior']:.4f} |"
        )
    lines.append("")
    lines.append(
        f"calibration E21       validation ECE pre-fit "
        f"{_pct(calibration['pre_calibration_ece_validation'])}"
        f"   sealed test ECE {_pct(calibration['test_ece'])}"
        f"   gate <= 5.00%: {calibration['ece_gate_ok']}"
    )
    before = split.get("weights_before_cap") or {}
    after = split.get("weights_after_cap") or {}
    lines.append(f"weights capped {split['n_weights_capped']} at {split['weight_cap']:.2f}"
                 f" ({split['weight_cap_rule']})"
                 f"   effective n {before.get('n_effective', 0.0):.1f} ->"
                 f" {after.get('n_effective', 0.0):.1f} of {after.get('n', 0)}")
    lines.append(f"abstentions skipped {split['n_abstain_skipped']}"
                 f"   unlabelled skipped {split['n_unlabelled_skipped']}")
    convergence = sections["convergence"]
    if not convergence.get("converged_flag"):
        lines.append(
            f"NOT CONVERGED         {convergence.get('epochs_run')} epochs at l2={convergence['l2']},"
            f" mean|grad| {convergence.get('mean_abs_grad')}"
            f" — raise --epochs before reading a threshold off this model"
        )
    else:
        lines.append(f"converged             {json.dumps(convergence, sort_keys=True)}")
    lines.append("top learned weights")
    for entry in sections["weights"][:10]:
        lines.append(f"  {entry['feature']:<26}{entry['weight']: .4f}")
    return lines


def _fit_markdown(report: FitReport) -> str:
    sections = report.sections
    lines = [f"# {report.title}", "", "## Headline", "", "```"]
    lines.extend(report.headline())
    lines.extend(["```", "", "## Split seal", "", "```",
                  json.dumps(sections["split"], indent=2, sort_keys=True), "```", "",
                  "## Learned weights", "", "| feature | weight | presence weight |",
                  "| --- | ---: | ---: |"])
    presence = {entry["feature"]: entry["weight"] for entry in sections["presence_weights"]}
    for entry in sections["weights"]:
        lines.append(f"| {entry['feature']} | {entry['weight']:.4f} | "
                     f"{presence.get(entry['feature'], 0.0):.4f} |")
    lines.extend(["", "## Interactions", "", "| left | right | weight |", "| --- | --- | ---: |"])
    for entry in sections["interactions"]:
        lines.append(f"| {entry['left']} | {entry['right']} | {entry['weight']:.4f} |")
    lines.extend(["", "## Reliability (sealed test split)", "",
                  "| bin | n | mean predicted | observed |", "| --- | ---: | ---: | ---: |"])
    for entry in sections["reliability_test"]:
        lines.append(f"| {entry['bin']} | {entry['n']} | {entry['mean_predicted']:.4f} | "
                     f"{entry['observed']:.4f} |")
    lines.append("")
    return "\n".join(lines)


def write_report(report: Report, path: str | Path) -> tuple[Path, Path]:
    """Write `<path>.json` and `<path>.md`; a suffix on `path` is ignored, never doubled."""
    base = Path(path)
    if base.suffix in (".json", ".md"):
        base = base.with_suffix("")
    base.parent.mkdir(parents=True, exist_ok=True)
    json_path = base.with_suffix(".json")
    markdown_path = base.with_suffix(".md")
    json_path.write_text(
        json.dumps(report.to_json(), indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    markdown_path.write_text(report.to_markdown(), encoding="utf-8")
    return json_path, markdown_path
