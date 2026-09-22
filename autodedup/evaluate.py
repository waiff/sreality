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
import inspect
import json
import math
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from autodedup import decide
from autodedup.decide import Decision
from autodedup.hazard_context import PairContext
from autodedup.labels import (
    CHEAP_TIERS,
    EMPTY_SAMPLE,
    Label,
    PairKey,
    Sample,
    Stratum,
    pair_key,
)
from autodedup.model import (
    CALIBRATION_METHODS,
    FIT_TOLERANCE,
    IRLS_TOLERANCE,
    LogisticModel,
    auc,
    expected_calibration_error,
    hand_initialised,
)
from autodedup.settings import Settings

Z_95: float = 1.96
ALPHA_95: float = 0.05

CERTIFICATE_CLASSES: tuple[str, ...] = ("K-A", "K-B", "K-C", "model")
MERGE_ZONE: str = "merge"
BAND_ZONE: str = "band"
REJECT_ZONE: str = "reject"
DECIDED_ZONES: tuple[str, ...] = (MERGE_ZONE, REJECT_ZONE)
# Not a zone `decide_pair` can return: the pairs the run never stored, which a recall denominator
# built from the run's own rows silently drops.
OFF_RUN_ZONE: str = "off_run"

# D3's per-stratum floor: a pooled 0.99 that hides a 0.90 stratum is not a passing gate.
STRATUM_FLOOR: float = 0.97
# The grid a shipped T_hi is chosen on. Finer than this a cut stops being a probability and
# becomes a rank: see `expressible_cuts`.
CUT_RESOLUTION: float = 1e-4
# No stratum earns a cut of its own off a handful of rows: under this many judged merges the
# search is describing noise, and a published cut invites someone to run it.
MIN_STRATUM_N: int = 30
# E68: how far a published cut must sit above the nearest labelled MUST-NOT-LINK below it. A
# search that stops as soon as the merge set is clean lands, BY CONSTRUCTION, one grid rung above
# the top labelled negative — a rank again rather than a probability, and the shape this program
# has now refused three times: D16 (iv) at 3e-5, D18/M43 (ii) at 7e-5, and W9's two dev-chosen
# cuts at 1.4e-5 (`model|same`, two stored pairs above an operator `same_project_different_unit`)
# and 1.0e-4 (`model|cross`, one stored pair above a gold `same_building_different_unit`), which
# between them bought five sealed false merges. Ten rungs of `CUT_RESOLUTION` is the smallest gap
# a refit's own score drift cannot close in silence.
CUT_MARGIN: float = 1e-3

# A stratum weight is a population ratio and the real draw runs to ~500x on the thinnest cell.
# Capping at a constant would re-order the design (a 4,000-pair stratum and an 80-pair one would
# land within a factor of two), so the cap is a MULTIPLE OF THE MEDIAN weight and the realised
# design effect is reported next to it — the cap is visible, never silent.
WEIGHT_CAP_MEDIAN_MULTIPLE: float = 10.0

BOOTSTRAP_DRAWS: int = 2000
BOOTSTRAP_SEED: int = 20260916

# Newton (IRLS) is the default optimiser: gradient descent needs thousands of epochs on ~100
# correlated columns and still stops short, and a shrunk weight vector moves the calibrated
# probability E21 reads its thresholds off. `gd` stays reachable — `--epochs` is how an
# under-fit model is produced deliberately.
FIT_METHODS: tuple[str, ...] = ("irls", "gd")
FIT_MAX_ITER: int = 50

# The l2 sweep W4c fits over. A single penalty is a guess about how much the ~86 identified terms
# should be shrunk; the grid makes it a MEASUREMENT (validation log-loss), and the whole grid is
# reported so a reader can see how flat — or how sharp — the optimum was.
# W4c's task grid is the first six; 0.3 and 1.0 extend it so the SELECTED penalty can be shown
# to be interior rather than sitting on the edge with the curve still falling. A boundary
# solution is reported as one (`l2_grid.chosen_is_edge`).
L2_TASK_GRID: tuple[float, ...] = (0.0003, 0.001, 0.003, 0.01, 0.03, 0.1)
L2_GRID: tuple[float, ...] = (*L2_TASK_GRID, 0.3, 1.0)

# Calibration is chosen out-of-fold INSIDE the validation split: fitting both maps on the fold and
# comparing their ECE on that same fold always picks isotonic, which has a knot per bin to spend.
CALIBRATION_FOLDS: int = 5
CALIBRATION_AUTO: str = "auto"

# The pooled ECE is a mid-range statistic and the auto-merge decision is not: every pair T_hi ever
# sees sits in the top of the ranking. So the bake-off also reports the ECE over the MERGE END —
# the rows the model itself ranks above this raw probability — and the CEILING of each map, because
# isotonic-over-bins cannot return more than its top bin's observed rate, and a T_hi above that
# ceiling switches the score layer off in silence.
MERGE_END_RAW: float = 0.90

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


# --- several draws, one design ---------------------------------------------------------------


@dataclass(slots=True)
class PooledSample(Sample):
    """Several judge draws collapsed into ONE design, with the frame behind every weight named.

    A pooled draw differs from a single `sample.json` in two ways a reader has to be able to see.
    Its per-pair stratum map is AUTHORITATIVE — each drawn pair is attributed to exactly one
    stratum (the one the newest draw that drew it used), so a pair cannot sit in two denominators
    while its single verdict feeds only one numerator. And its populations come from frames that
    do not agree: the draws were stamped against different engine runs, so `frame_conflicts`
    carries every stratum where the frames disagreed, what was chosen, and what that did to the
    weight."""

    draws: tuple[dict[str, Any], ...] = ()
    order_source: str = ""
    frame_conflicts: tuple[dict[str, Any], ...] = ()

    def weight_for(self, key: PairKey, stratum: str | None = None,
                   default: float = 1.0) -> float:
        """The POOLED attribution wins over the judgement row's stamp.

        The stamp names the cell the pair was drawn in *by one draw*; the pooled design re-cut the
        cells across all of them, and using the stamp here would put the numerator in one cell and
        the denominator in another."""
        name = self.pair_stratum.get(key)
        return self.weight_for_name(name if name is not None else stratum, default)

    def to_json(self) -> dict[str, Any]:
        return {
            **super().to_json(),
            "pooled_draws": list(self.draws),
            "population_frame_order": self.order_source,
            "n_frame_conflicts": len(self.frame_conflicts),
            "frame_conflicts": list(self.frame_conflicts),
        }


def stratum_of_pair(draw: Sample, key: PairKey, stamped: str | None) -> str:
    """The design cell one judged pair belongs to, under whatever sample is in force.

    A POOLED draw has already attributed every pair it holds, and that attribution is the one the
    denominators were counted under. A single draw has not: there the judgement row's own stamp is
    the sampler's word and the file's per-pair map is only the fallback."""
    if isinstance(draw, PooledSample):
        name = draw.pair_stratum.get(key)
        if name is not None:
            return name
    return stamped or draw.stratum_of(key) or "(none)"


_DRAW_ID = re.compile(r"(\d{6,})")


def draw_id(draw: Sample) -> int | None:
    """The draw's own recency key, read off the lane artifact id its path carries.

    Never the order the caller happened to pass them in: which frame a stratum's population comes
    from decides every HT weight, and `--judgements A B` and `--judgements B A` must not be
    different estimates."""
    found = _DRAW_ID.findall(str(draw.path or ""))
    return int(found[-1]) if found else None


def pooled_sample(draws: Sequence[Sample]) -> Sample:
    """Several judge draws collapsed into ONE set of Horvitz-Thompson weights (W4f).

    Each `sample.json` describes the draw it came from, and a stratum's rate is a property of
    that draw, not of its name: `merge|K-C|jablonec|cross` was drawn 87 of 1,178 in the seed-1
    vision sample and 47 of 1,175 in the seed-2 one. Weighting a pooled label set by whichever
    file happened to be passed first therefore inflates every pair the other draws contributed —
    the label store holds one verdict per pair, but the design behind it is the UNION of the
    draws.

    The union is what is weighted here, and it is an equal-probability sample within a stratum:
    independent draws of `n_1, n_2, ...` from one stratum of `N` include every member with the
    same probability `1 - prod(1 - n_d/N)`, so the pooled rate is simply DISTINCT drawn pairs
    over the population — which is also what makes a sub-draw free (gold-600 is 600 of the same
    1,500 pairs the vision draw judged, and contributes no new denominator). Each pair is counted
    ONCE, in the stratum the newest draw that drew it used, so a pair whose zone moved between two
    runs cannot inflate a cell that has no verdict to put in the numerator.

    Populations come from the newest frame that names the stratum, ordered by `draw_id` — the lane
    artifact id, not the argument order. Where the newest frame cannot hold the pairs actually
    drawn the stratum is a FRAME CONFLICT, not a rounding problem: the widest frame that named it
    is used (a stratum may not stand for less of the cohort than another frame says it holds), and
    every such stratum is recorded on `frame_conflicts` with the weight it ended up with, because
    silently taking the drawn count would set that cell's inflation factor to 1.0."""
    usable = [draw for draw in draws if draw is not None]
    if not usable:
        return EMPTY_SAMPLE
    if len(usable) == 1:
        return usable[0]
    seen_populations: dict[str, dict[int, int]] = {}
    for index, draw in enumerate(usable):
        for name, stratum in draw.strata.items():
            if stratum.n_total:
                seen_populations.setdefault(name, {})[index] = stratum.n_total
    disagreeing = sorted(
        name for name, by_draw in seen_populations.items() if len(set(by_draw.values())) > 1
    )
    ids = [draw_id(draw) for draw in usable]
    if all(value is not None for value in ids):
        order = sorted(range(len(usable)), key=lambda index: (ids[index], index))
        order_source = "draw id (the lane artifact id on the sample path)"
    elif not disagreeing:
        order = list(range(len(usable)))
        order_source = "declaration order (immaterial: no stratum's population disagrees)"
    else:
        unnamed = [str(usable[i].path) for i, value in enumerate(ids) if value is None]
        raise ValueError(
            f"cannot pool these draws: {len(disagreeing)} strata have disagreeing populations "
            f"and {unnamed} carry no draw id, so which frame wins would be decided by the order "
            f"they were passed in. Pass the lane's own sample.json paths (they carry the run id)."
        )

    drawn: dict[str, set[PairKey]] = {}
    pair_stratum: dict[PairKey, str] = {}
    # Newest first: the first draw that names a pair owns it, and every other name that draw set
    # keeps only the pairs no newer draw claimed.
    for index in reversed(order):
        for key, name in usable[index].pair_stratum.items():
            if key in pair_stratum:
                continue
            pair_stratum[key] = name
            drawn.setdefault(name, set()).add(key)

    strata: dict[str, Stratum] = {}
    conflicts: list[dict[str, Any]] = []
    for name in sorted(set(seen_populations) | set(drawn)):
        selected = len(drawn.get(name, ()))
        by_draw = seen_populations.get(name, {})
        newest = next((by_draw[index] for index in reversed(order) if index in by_draw), 0)
        total = newest
        if selected and total < selected:
            widest = max(by_draw.values(), default=0)
            total = max(widest, selected)
            conflicts.append({
                "stratum": name,
                "n_selected_pooled": selected,
                "population_newest_frame": newest,
                "population_by_draw": {
                    str(usable[index].path or index): by_draw[index]
                    for index in order if index in by_draw
                },
                "population_used": total,
                "weight_used": (total / selected) if selected else 0.0,
                "weight_under_newest_frame": (newest / selected) if selected else 0.0,
                "resolution": ("widest frame" if total == widest and widest >= selected
                               else "collapsed to the drawn count (weight 1.0)"),
            })
        strata[name] = Stratum(name, selected, max(total, selected))

    note = f"pooled({len(usable)} draws; frames by {order_source}"
    if disagreeing:
        note += f"; {len(disagreeing)} strata with disagreeing populations, newest frame wins"
    if conflicts:
        note += f"; {len(conflicts)} FRAME CONFLICTS (see frame_conflicts)"
    note += ")"
    return PooledSample(
        strata=strata,
        pair_stratum=pair_stratum,
        seed=None,
        tier="+".join(sorted({draw.tier for draw in usable if draw.tier})) or None,
        judge_version=next(
            (draw.judge_version for draw in usable if draw.judge_version), None
        ),
        n_requested=sum(draw.n_requested or 0 for draw in usable),
        n_selected=len(pair_stratum),
        path="; ".join(str(draw.path) for draw in usable if draw.path),
        stratum_fn=note,
        draws=tuple(
            {"path": str(usable[index].path), "draw_id": ids[index],
             "seed": usable[index].seed, "tier": usable[index].tier,
             "n_selected": len(usable[index].pair_stratum),
             "n_cohort": usable[index].n_cohort}
            for index in order
        ),
        order_source=order_source,
        frame_conflicts=tuple(conflicts),
    )


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


# --- re-deciding a stored run through the LIVE rule ----------------------------------------

# Two windows that overlap and two that do not, so `decide.disjoint_windows` — the one clause of
# K-B a stored pair row does not carry — can be replayed as the run recorded it.
OVERLAPPING_WINDOW: tuple[str, str] = ("2020-01-01T00:00:00+00:00", "2020-12-31T00:00:00+00:00")
DISJOINT_WINDOW: tuple[str, str] = ("2021-01-01T00:00:00+00:00", "2021-06-30T00:00:00+00:00")


class _SimSide:
    """One side of a pair as `decide_pair` may interrogate it, rebuilt from the stored row.

    Everything the row does not carry reads as None — E12's "unknown", which no guard and no
    certificate may treat as agreement — so a clause `decide` grows tomorrow degrades to
    abstention here instead of silently inventing evidence."""

    def __init__(self, listing_id: int, window: tuple[str, str]) -> None:
        self.listing_id = int(listing_id)
        self.first_seen_at, self.last_seen_at = window
        self.inactive_at = None

    def __getattr__(self, name: str) -> None:
        if name.startswith("__"):
            raise AttributeError(name)
        return None


def _sim_sides(row: Mapping[str, Any]) -> tuple["_SimSide", "_SimSide"]:
    """The two listing stubs a stored row can stand in for, windowed as the run recorded K-B."""
    lo, hi = int(row["lo"]), int(row["hi"])
    window = DISJOINT_WINDOW if str(row.get("certificate") or "") == "K-B" else OVERLAPPING_WINDOW
    return _SimSide(lo, OVERLAPPING_WINDOW), _SimSide(hi, window)


def _merge_zone_gate(row: Mapping[str, Any], settings: Settings) -> str | None:
    """Ask the LIVE E45/E46 gate whether this pair may enter the merge zone at all.

    The gate's arity belongs to `decide`, which reads the pair from the features alone today and
    took the two listings yesterday; binding to whichever it currently declares keeps this a call
    into that module rather than a second copy of the rule that drifts out of step with it."""
    feats = _feats(row)
    if len(inspect.signature(decide.merge_zone_block).parameters) <= 2:
        return decide.merge_zone_block(feats, settings)
    left, right = _sim_sides(row)
    return decide.merge_zone_block(feats, left, right, settings)


def decide_context(
    row: Mapping[str, Any], settings: Settings | None = None
) -> dict[str, Any]:
    """What `decide_pair` knew about this pair, read back off the stored row (E11/E22/E24).

    `blocked` is the layer the score cannot reach in EITHER direction: a guard veto, an
    auto-reject, or a merge-zone gate (E45/E46) — all three are functions of the pair rather than
    of the cut, so a pair carrying one sits outside the merge set at every T_hi. The gate is asked
    of `decide.merge_zone_block` rather than parsed out of the stored `reason`, because a gated
    pair that also scored below T_hi was written to the run as a plain `model` band row: its
    reason says nothing, and a threshold search reading reasons would count it back in.

    `diverse` is the E11 evidence gate that demotes a pair to the band however high it scores; a
    certificate merges at any score at all. A search that ignores the three describes a decision
    rule the engine does not run."""
    reason = str(row.get("reason") or "")
    live = settings if settings is not None else Settings()
    discarded = bool(row.get("veto")) or reason.startswith("auto_reject")
    certificate = (str(row["certificate"]) if row.get("certificate") else None)
    gate: str | None = None
    propose_only = False
    if not discarded:
        gate = _merge_zone_gate(row, live)
        # E48: a stratum shipped propose-only merges at no cut at all, certificate or not, so it
        # belongs in `blocked` beside the gates — and NOT in `discarded`, because the pair is
        # still proposed in the band.
        feats = _feats(row)
        # The row carries the side as a FACT (`cross_source`); the stored feature vector is the
        # engine's copy of it. Prefer the feature, fall back to the fact, so a stub row and a real
        # one land in the same cell as `decide_stratum` names it.
        feats.setdefault("same_source", (0.0 if row.get("cross_source") else 1.0, True))
        propose_only = decide.stratum_t_hi(feats, certificate, live) is None
    return {
        "certificate": certificate,
        "diverse": len(row.get("families") or ()) >= live.min_evidence_families,
        "blocked": discarded or gate is not None or propose_only,
        "stratum_propose_only": propose_only,
        # A guard and an auto-reject take the pair out of the QUEUE; E45/E46 and E11 only take it
        # out of the merge zone, and the operator still sees it in the band. Recall spends the
        # first against its budget and must not spend the second.
        "discarded": discarded,
        "merge_zone_block": gate,
    }


def resimulate(
    row: Mapping[str, Any], model: LogisticModel, settings: Settings
) -> Decision:
    """Re-run THIS pair through `decide.decide_pair` with a new model — never a copy of its logic.

    The two layers a stored row cannot reproduce are replayed from what the run recorded rather
    than re-derived: a guard veto is a fact about the two listings that no threshold and no model
    can move, and K-B's disjoint-window clause is the one certificate input the row omits, so the
    stub windows are made disjoint exactly when the run's certificate was K-B. Everything else —
    the auto-rejects, K-A, K-C, the E11 diversity gate, the zone cuts — is recomputed by the live
    module, so a rule Task A changes changes this simulation too."""
    lo, hi = int(row["lo"]), int(row["hi"])
    veto = row.get("veto")
    feats = _feats(row)
    if veto:
        return Decision(lo, hi, "veto", 0.0, set(), None, str(veto), f"guard:{veto}")
    left, right = _sim_sides(row)
    return decide.decide_pair(
        left, right, left, right, feats, list(row.get("probes") or ()), model, settings,
        PairContext.from_json(row.get("context")),
    )


def rescore_rows(
    rows: Sequence[Mapping[str, Any]], model: LogisticModel, settings: Settings
) -> list[dict[str, Any]]:
    """The stored run re-decided with a different model — same pairs, new score/zone/reason."""
    out: list[dict[str, Any]] = []
    for row in rows:
        decision = resimulate(row, model, settings)
        out.append({
            **row,
            "score": decision.score,
            "zone": decision.zone,
            "certificate": decision.certificate,
            "families": sorted(decision.families),
            "veto": decision.veto,
            "reason": decision.reason,
        })
    return out


def decide_stratum(row: Mapping[str, Any]) -> str:
    """The stratification D3's per-stratum floor is read on: deciding layer x source side.

    Certificate and model are different precision regimes (one structural, one learned) and the
    same/cross split is the one contrast the operator's false-merge story turns on (a same-portal
    developer re-advert is the K-A failure mode). Block is NOT in the key: four blocks would cut
    every cell below the n a 0.99 lower bound can even reach."""
    return f"{certificate_class(row)}|{_side(row)}"


def effective_t_hi(
    settings: Settings,
    stratum_key: str,
    table: Mapping[str, float | None] | None = None,
) -> float | None:
    """The cut THIS stratum merges at: its override when it has one, else the global `t_hi`.

    `None` is not "missing" — it is PROPOSE-ONLY (D3): the stratum could not prove 0.99, so no
    score may merge it and the zone rule must send it to the band. The override table lives on
    `settings.t_hi_by_stratum` when the row grows the column, and is passed in until it does."""
    overrides = table if table is not None else getattr(settings, "t_hi_by_stratum", None)
    if overrides and stratum_key in overrides:
        value = overrides[stratum_key]
        return None if value is None else float(value)
    return settings.t_hi


def labels_needed_for_lb(precision_lb: float, z: float = Z_95) -> int:
    """How many CONSECUTIVE correct judged merges a stratum needs before it can clear the bar.

    With k = n the Wilson lower bound is exactly `n / (n + z^2)`, so the gate is a statement about
    label volume before it is one about the model: at 0.99 no stratum passes under ~381 judged
    merges however perfect it is. Reporting this beside a `None` t_hi is the difference between
    "the model is not good enough" and "the sample cannot answer the question"."""
    if not 0.0 < precision_lb < 1.0:
        raise ValueError(f"precision_lb must be in (0, 1): {precision_lb}")
    n = max(1, int(math.ceil(precision_lb * z * z / (1.0 - precision_lb))))
    while wilson_lower(n, n, z) < precision_lb:
        n += 1
    return n


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
    # `blocked` is "no cut merges this pair"; `discarded` is the stronger "no cut even PROPOSES
    # it" — a guard or an auto-reject. Only the second is a positive that recall has truly lost.
    discarded: bool = False
    split: str = "train"
    # The SAMPLER's stratum (`stratum`) is the design cell the HT weight belongs to; `cell` is the
    # deciding layer x source side D3's per-stratum floor is read on. They are different questions
    # and a single field would answer neither.
    cell: str = "(none)"

    @property
    def merged_regardless(self) -> bool:
        return (not self.blocked) and self.diverse and self.certificate is not None

    @property
    def score_governed(self) -> bool:
        """The pairs a score cut actually decides: no certificate, not blocked, diverse."""
        return (not self.blocked) and self.diverse and self.certificate is None

    def merged_at(
        self,
        t: float,
        *,
        zone_rule: bool,
        stratum_t_hi: Mapping[str, float | None] | None = None,
    ) -> bool:
        """`stratum_t_hi` is E48's table, read exactly as `decide.stratum_t_hi` reads it: this
        pair's own cell's cut when it has one, `None` meaning PROPOSE-ONLY — which holds back the
        certificates too, because `decide_pair` consults the table before it consults them."""
        cut: float | None = t
        if stratum_t_hi is not None and self.cell in stratum_t_hi:
            value = stratum_t_hi[self.cell]
            cut = None if value is None else float(value)
        if not zone_rule:
            return cut is not None and self.score >= cut
        if self.blocked or not self.diverse or cut is None:
            return False
        return self.certificate is not None or self.score >= cut


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
            scored.discarded = bool(context.get("discarded", scored.blocked))
        out.append(scored)
    return out


def _gates(k: int, n: int, precision_lb: float, precision_point: float) -> bool:
    return bool(n) and k / n >= precision_point and wilson_lower(k, n) >= precision_lb


def expressible_cuts(
    scores: Iterable[float], resolution: float | None = CUT_RESOLUTION
) -> list[float]:
    """Candidate cuts a shipped settings row can state as a PROBABILITY, descending.

    A calibration map is flat over long runs and near-vertical between them, so hundreds of pairs
    can share a probability interval 1e-9 wide. A search over every distinct score will happily
    stop inside such an interval — and then the published cut is not "merge above 98.6 %", it is
    "merge these 81 pairs and not those nine", chosen at a resolution no operator can read and no
    later run can reproduce. Measured on W4f's own cell: 0.98625585 merges 29 sealed pairs with 2
    errors and 0.9862558542015111 merges 20 with none, a 4e-10 apart.

    Quantising to `resolution` gives each score two candidates — the grid point just below it
    (which admits it) and the one just above (which does not) — so every cut the search can
    return is a real number of decimals. `None` restores the raw per-score search."""
    values = [float(score) for score in scores]
    if resolution is None or resolution <= 0.0:
        return sorted({value for value in values}, reverse=True)
    out: set[float] = set()
    for value in values:
        steps = value / resolution
        out.add(round(math.floor(steps) * resolution, 12))
        out.add(round(math.ceil(steps) * resolution, 12))
    return sorted(out, reverse=True)


def nearest_negative_below(
    cut: float, rows: Sequence[ScoredRow]
) -> tuple[float, float] | None:
    """The top-scoring labelled NEGATIVE the cut leaves below it, and how far below (E68).

    Only the score-governed rows count: a certificate merges at every cut, so a certificate
    negative is not a statement about where the cut sits. `None` means the cell holds no labelled
    negative under the cut at all — the margin is then unmeasured, not infinite, and the caller
    says which of the two it is."""
    below = [row.score for row in rows if row.score_governed and row.y == 0 and row.score < cut]
    if not below:
        return None
    top = max(below)
    return top, cut - top


def _t_hi_search(
    rows: Sequence[ScoredRow],
    precision_lb: float,
    precision_point: float,
    *,
    zone_rule: bool = True,
    resolution: float | None = CUT_RESOLUTION,
    min_margin: float = 0.0,
) -> tuple[float | None, int, int]:
    """Smallest EXPRESSIBLE cut whose SIMULATED merge set clears both gates; (t, k, n) there.

    Simulated, not `score >= t`: certificate pairs sit in the merge set at every cut and blocked
    or single-family pairs sit outside it at every cut, so the cut only ever moves the
    score-governed pairs in and out. Precision is not monotone in t, so every candidate cut is
    tried and the smallest winner kept — over the grid `expressible_cuts` returns, never over the
    raw scores, which is how a threshold ends up fitted to individual holdout pairs.

    `min_margin` (E68) is the third gate: a cut that clears both rates by stopping one rung above
    the top labelled must-not-link is fitted to that one pair, and the search will do exactly that
    unless it is told not to. It binds only on a cut that admits score-governed rows — a cut of
    1.0 over a cell whose merges are all certificates decides nothing by score, so there is
    nothing for a negative to sit under."""
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
    for cut in expressible_cuts((row.score for row in movable), resolution):
        while index < len(ordered) and ordered[index].score >= cut:
            k += ordered[index].y
            n += 1
            index += 1
        if not _gates(k, n, precision_lb, precision_point):
            continue
        if min_margin > 0.0 and index:
            gap = nearest_negative_below(cut, movable)
            if gap is not None and gap[1] < min_margin:
                continue
        best = (cut, k, n)
    return best


def merge_set(
    rows: Sequence[ScoredRow],
    t: float,
    *,
    zone_rule: bool = True,
    stratum_t_hi: Mapping[str, float | None] | None = None,
) -> list[ScoredRow]:
    return [
        row for row in rows
        if row.merged_at(t, zone_rule=zone_rule, stratum_t_hi=stratum_t_hi)
    ]


def _per_stratum_precision(
    rows: Sequence[ScoredRow], *, by: str = "cell"
) -> dict[str, dict[str, Any]]:
    """Precision per group, keyed on the DECIDING CELL by default.

    D3's floor ("no stratum below 0.97") is a statement about `K-C|cross`, `model|same` — the
    cells E48 ships or holds back — not about the sampler's `merge|K-C|vysocany|cross`, which is a
    design cell that nothing in the shipped package is read at. Both are published; only the cell
    grain answers the gate."""
    grouped: dict[str, list[ScoredRow]] = {}
    for row in rows:
        grouped.setdefault(row.cell if by == "cell" else row.stratum, []).append(row)
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
    stratum_t_hi: Mapping[str, float | None] | None = None,
) -> dict[str, Any]:
    """Score a FIXED (T_hi, T_lo) over rows that had no say in choosing it — §6's sealed split.

    The same arithmetic as the search, run once: the point is that the rows are different."""
    scored = _normalise_scored(rows)
    if not scored:
        return {"n": 0, "t_hi": t_hi, "t_lo": t_lo, "precision": None,
                "n_merge": 0, "n_positive": 0}
    # A propose-only T_hi is not "nothing to measure": no SCORE merges a pair, but the certificates
    # still do, and the recall the band has to carry is still a number the sealed split can report.
    # An infinite cut is exactly that rule, so the arithmetic below stays one code path.
    cut = math.inf if t_hi is None else t_hi
    merged = merge_set(scored, cut, zone_rule=zone_rule, stratum_t_hi=stratum_t_hi)
    k, n = sum(row.y for row in merged), len(merged)
    per_stratum = _per_stratum_precision(merged)
    per_sampler_stratum = _per_stratum_precision(merged, by="stratum")
    floors = [body["precision"] for body in per_stratum.values() if body["precision"] is not None]
    sampler_floors = [
        body["precision"] for body in per_sampler_stratum.values()
        if body["precision"] is not None
    ]
    weight_positive = sum(row.weight for row in scored if row.y == 1)
    missed = sum(
        row.weight for row in scored
        if row.y == 1
        and not row.merged_at(cut, zone_rule=zone_rule, stratum_t_hi=stratum_t_hi)
        and (row.score <= t_lo or (zone_rule and row.discarded))
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
        "per_stratum_precision_basis": "deciding cell (certificate|side) — D3's floor",
        "per_sampler_stratum_precision": per_sampler_stratum,
        "min_stratum_precision": min(floors) if floors else None,
        "min_sampler_stratum_precision": min(sampler_floors) if sampler_floors else None,
        "stratum_floor": stratum_floor,
        "stratum_floor_ok": (min(floors) >= stratum_floor) if floors else None,
        "stratum_t_hi_applied": (
            None if stratum_t_hi is None else dict(sorted(stratum_t_hi.items()))
        ),
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
            "per_stratum_precision_basis": "deciding cell (certificate|side) — D3's floor",
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
    # positive the E11 or E45/E46 gates demoted is NOT unavoidable: those send it to the band,
    # where the operator sees it, and T_lo still decides whether it gets there.
    unavoidable = sum(row.weight for row in positives if zone_rule and row.discarded)
    governed = sorted(
        (row for row in positives
         if not (zone_rule and (row.discarded or row.merged_regardless))),
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


def stratum_thresholds(
    dev_rows: Sequence[ScoredRow],
    sealed_rows: Sequence[ScoredRow],
    *,
    precision_lb: float = 0.99,
    precision_point: float = 0.995,
    stratum_floor: float = STRATUM_FLOOR,
    zone_rule: bool = True,
    min_n: int = MIN_STRATUM_N,
    fitted_on: str = "train+validation",
    allow_overrides: bool = True,
    resolution: float | None = CUT_RESOLUTION,
    min_margin: float = CUT_MARGIN,
) -> dict[str, Any]:
    """A T_hi per deciding stratum, CHOSEN on dev and MEASURED on the sealed split (D3/E22-E25).

    A stratum that cannot clear both gates gets `None` — propose-only: no score merges it and the
    zone rule must send it to the band. Beside every `None` sits the reason it is one, because the
    two reasons demand opposite work: `precision` (the model merges wrong pairs there) is a
    modelling problem, `sample` (even a flawless cell cannot reach a 0.99 lower bound at this n)
    is a labelling budget. The relaxed cut — smallest cut clearing the POINT estimate alone — and
    the lower bound achieved there make the gap explicit.

    `min_n` is the floor under BOTH cuts: a cell whose merge set is thinner than that gets no
    threshold of its own at all. The 0.99 gate enforces it by accident (it needs ~381 merges); the
    relaxed cut, searched with no lower bound, would otherwise publish a cut read off two rows.

    `min_margin` is E68: the two rate gates above say the merge set is clean, and a search
    satisfying them alone stops one grid rung above the top labelled must-not-link — so the cut
    is read off that one pair and a refit's own drift walks it back over. The cell reports what
    the margin cost (`t_hi_without_margin`) and which pair it is measured against, because a cut
    withheld on margin is a labelling question, not a modelling one.

    `allow_overrides=False` refuses every threshold whatever the arithmetic says — the honest
    answer when `dev_rows` are not held out from `sealed_rows`."""
    needed = labels_needed_for_lb(precision_lb)
    by_cell: dict[str, list[ScoredRow]] = {}
    for row in dev_rows:
        by_cell.setdefault(row.cell, []).append(row)
    sealed_by_cell: dict[str, list[ScoredRow]] = {}
    for row in sealed_rows:
        sealed_by_cell.setdefault(row.cell, []).append(row)

    strata: dict[str, Any] = {}
    overrides: dict[str, float | None] = {}
    for cell in sorted(set(by_cell) | set(sealed_by_cell)):
        members = by_cell.get(cell, [])
        t_hi, k_hi, n_hi = _t_hi_search(
            members, precision_lb, precision_point, zone_rule=zone_rule, resolution=resolution,
            min_margin=min_margin,
        )
        unguarded_t, _, _ = _t_hi_search(
            members, precision_lb, precision_point, zone_rule=zone_rule, resolution=resolution
        )
        relaxed_t, relaxed_k, relaxed_n = _t_hi_search(
            members, 0.0, precision_point, zone_rule=zone_rule, resolution=resolution,
            min_margin=min_margin,
        )
        sealed = sealed_by_cell.get(cell, [])
        at = t_hi if t_hi is not None else relaxed_t
        # No reachable cut is not "nothing merges here": a certificate stratum merges at every
        # cut, so an infinite one is what the sealed split must be measured under.
        sealed_merged = merge_set(
            sealed, at if at is not None else math.inf, zone_rule=zone_rule
        )
        sealed_k, sealed_n = sum(row.y for row in sealed_merged), len(sealed_merged)
        # What the stratum merges with no score cut at all: its certificates. A certificate
        # stratum that fails the POINT gate fails it here, at every cut, which is why the
        # propose-only reason for those cells is "precision" and not "sample".
        floor_rows = [row for row in members if row.merged_regardless]
        floor_k, floor_n = sum(row.y for row in floor_rows), len(floor_rows)
        # A cell whose relaxed merge set is under `min_n` has not measured anything: its cut is
        # published as a diagnostic, never as a candidate to ship.
        relaxed_thin = relaxed_t is not None and relaxed_n < min_n
        # The margin gate is the only one that can withhold a cut BOTH rate gates cleared, so
        # whether it is what bound is recorded before `min_n` gets its say.
        margin_withheld = t_hi is None and unguarded_t is not None and min_margin > 0.0
        if t_hi is not None and n_hi < min_n:
            t_hi = None
        margin_gap = (
            nearest_negative_below(t_hi, members) if t_hi is not None else None
        )
        reason: str | None = None
        if t_hi is None:
            # "sample" and "precision" demand opposite work — more labels, or a better rule — so
            # the certificate floor decides between them: a cell whose certificates already merge
            # wrong pairs cannot be fixed by labelling, and one that is flawless but thin can.
            floor_fails = bool(floor_n) and (floor_k / floor_n) < precision_point
            reason = (
                "precision" if floor_fails
                else ("sample" if (relaxed_t is not None or floor_n < needed) else "precision")
            )
            # A cut the rates would have published and the margin withheld is neither of those
            # two: the rule is good enough and the sample is deep enough, and what is missing is
            # daylight between the cut and one labelled must-not-link.
            if margin_withheld:
                reason = "margin"
        if not allow_overrides:
            t_hi = None
            reason = "no dev split"
        overrides[cell] = t_hi
        strata[cell] = {
            "n_dev": len(members),
            "n_dev_positive": sum(row.y for row in members),
            "t_hi": t_hi,
            "n_at_t_hi": n_hi,
            "precision_at_t_hi": (k_hi / n_hi) if n_hi else None,
            "wilson_lb_at_t_hi": wilson_lower(k_hi, n_hi) if n_hi else None,
            "min_margin": min_margin,
            "t_hi_without_margin": unguarded_t,
            "margin_withheld": margin_withheld,
            "margin_at_t_hi": None if margin_gap is None else margin_gap[1],
            "nearest_negative_below_t_hi": None if margin_gap is None else margin_gap[0],
            "relaxed_t_hi": relaxed_t,
            "relaxed_insufficient_n": relaxed_thin,
            "min_n": min_n,
            "n_at_relaxed": relaxed_n,
            "precision_at_relaxed": (relaxed_k / relaxed_n) if relaxed_n else None,
            "wilson_lb_at_relaxed": wilson_lower(relaxed_k, relaxed_n) if relaxed_n else None,
            "n_certificate_merges": floor_n,
            "precision_certificate_merges": (floor_k / floor_n) if floor_n else None,
            "wilson_lb_certificate_merges": wilson_lower(floor_k, floor_n) if floor_n else None,
            "labels_needed_for_lb": needed,
            # A cell can fail BOTH ways at once — 97/98 is under the point gate and also far too
            # thin to tell 0.99 from 0.995 apart — so the reason names the binding constraint and
            # this says whether more labels alone could ever settle it.
            "sample_sufficient": max(relaxed_n, floor_n) >= needed,
            "propose_only": t_hi is None,
            "propose_only_reason": reason,
            "sealed_n": len(sealed),
            "sealed_n_merge": sealed_n,
            "sealed_n_positive": sealed_k,
            "sealed_precision": (sealed_k / sealed_n) if sealed_n else None,
            "sealed_wilson_lb": wilson_lower(sealed_k, sealed_n) if sealed_n else None,
            "sealed_weighted_precision": hajek_rate(
                [(row.stratum, row.y, row.weight) for row in sealed_merged]
            ),
            # The gate below counts heads; the HT rate above weights them. On 7-12 sealed rows
            # neither is stable, so the design effect that separates them rides alongside instead
            # of being left for a reader to infer.
            "sealed_design": design_effect([row.weight for row in sealed_merged]),
            "sealed_floor_ok": (
                None if not sealed_n else (sealed_k / sealed_n) >= stratum_floor
            ),
            "measured_at": (
                "t_hi" if t_hi is not None
                else ("relaxed_t_hi (diagnostic)" if relaxed_t is not None
                      else "certificates only (no reachable cut)")
            ),
        }
    shipping = [cell for cell, value in overrides.items() if value is not None]
    return {
        "criteria": {
            "precision_lb": precision_lb, "precision_point": precision_point,
            "stratum_floor": stratum_floor, "min_n": min_n, "min_margin": min_margin,
            # Named rather than left implicit: the gate counts JUDGED PAIRS (Wilson on k/n) while
            # the target it is compared against is a cohort rate. `sealed_weighted_precision` and
            # `sealed_design` beside it are what say whether the two agree on this cell.
            "gate_basis": "unweighted judged counts (Wilson LB); HT rate reported alongside",
            "cut_resolution": resolution,
        },
        "key": "certificate|side",
        "fitted_on": fitted_on,
        "measured_on": "test (sealed)" if allow_overrides else "test (same rows as the fit)",
        "labels_needed_for_lb": needed,
        "t_hi_by_stratum": overrides,
        "n_strata": len(strata),
        "n_auto_merge_strata": len(shipping),
        "auto_merge_strata": sorted(shipping),
        "propose_only": sorted(cell for cell in overrides if overrides[cell] is None),
        "strata": strata,
    }


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
    split_map: Mapping[int, int] | None = None,
    expect_seal: str | None = None,
) -> EvalReport:
    """Every §9 number this program can compute from one run plus one set of judgements.

    `split_map` is the fit's own partition, and passing it is what makes "the sealed split" mean
    the same rows here as it did there. Without it the split is re-derived from THIS run's merge
    edges — which are a function of the model and the thresholds under evaluation, so a challenger
    silently re-randomises the holdout and half the comparison is measured on rows the model was
    trained on. `expect_seal` (a prefix of the seal a model artifact carries) turns that from a
    convention into a check."""
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

    groups = dict(split_map) if split_map is not None else split_groups(run_pairs, labels)
    seal = split_seal(groups)
    if expect_seal and not seal["sha256"].startswith(str(expect_seal)):
        raise ValueError(
            f"split seal mismatch: the model was fitted on {expect_seal}, this map is "
            f"{seal['sha256'][:16]} — pass the fit's split_map.json, never a re-derived split"
        )
    n_rows = 0
    n_labelled = 0
    n_defaulted_weight = 0
    weight_positive_total = 0.0
    weight_positive_below_t_lo = 0.0
    weight_positive_in_band = 0.0
    weight_positive_discarded = 0.0
    weight_positive_gated = 0.0
    # The zone the ENGINE decided, which is the only honest denominator for recall: a score-based
    # line cannot see the gates (E45/E46/E47/E11) or the certificates, and understates the cost of
    # the rule floor by the whole of what they demote.
    weight_positive_by_zone: dict[str, float] = {}
    developer_suspected = 0

    seen: set[PairKey] = set()
    for row in run_pairs:
        key = pair_key(row["lo"], row["hi"])
        seen.add(key)
        n_rows += 1
        zone = str(row.get("zone") or "?")
        label = labels.get(key)
        # The judgement row carries the stratum the SAMPLER stamped on it; a POOLED draw has
        # re-cut the cells across every draw and its attribution wins (see `stratum_of_pair`).
        stratum = stratum_of_pair(draw, key, label.stratum if label else None)
        weight = draw.weight_for_name(stratum)
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
        context = decide_context(row, settings)
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
            cell=decide_stratum(row),
        ))
        if label.y == 1:
            weight_positive_total += weight
            weight_positive_by_zone[zone] = weight_positive_by_zone.get(zone, 0.0) + weight
            if context["discarded"]:
                weight_positive_discarded += weight
            elif context["merge_zone_block"] or not context["diverse"]:
                weight_positive_gated += weight
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

    # A labelled pair this run never STORED is still a labelled pair, and a positive among them
    # is a missed duplicate — one the engine cannot even propose. Leaving them out of the recall
    # denominator (the W4f defect) makes a calibration that pushes pairs under `store_floor` look
    # like it improved recall: run3pre left 102 labels off the run, the isotonic_pav row 1,159.
    # They are a FOURTH zone, not a footnote, and they cannot be split into "below the floor" and
    # "never a candidate" from the run alone, so the zone says both.
    weight_positive_on_run = weight_positive_total
    off_run = sorted(set(labels) - seen)
    off_run_judged = off_run_positive = 0
    for key in off_run:
        label = labels[key]
        stratum = stratum_of_pair(draw, key, label.stratum)
        weight = draw.weight_for_name(stratum)
        zones.setdefault(OFF_RUN_ZONE, Cell()).add(label, weight, stratum)
        if label.y is None:
            continue
        off_run_judged += 1
        if label.y == 1:
            off_run_positive += 1
            weight_positive_total += weight
            weight_positive_by_zone[OFF_RUN_ZONE] = (
                weight_positive_by_zone.get(OFF_RUN_ZONE, 0.0) + weight
            )

    merge_pooled = Cell()
    for cell in merge_by_certificate.values():
        merge_pooled.absorb(cell)

    population = [_score(row) for row in run_pairs]
    n_band_zone = sum(1 for row in run_pairs if str(row.get("zone") or "") == BAND_ZONE)
    dev = [row for row in scored if row.split in DEV_SPLITS]
    sealed = [row for row in scored if row.split == "test"]
    criteria = {"population_scores": population, "store_floor": settings.store_floor,
                "draws": draws}
    threshold_report = thresholds(dev or scored, **criteria)
    threshold_all = thresholds(scored, **criteria)
    holdout = measure_thresholds(
        sealed, threshold_report.t_hi, threshold_report.t_lo, draws=draws
    )
    # The searched cut is what D3 asks about; the LOADED row is what the engine is running right
    # now. They are different questions and a report that answers only the first cannot say what
    # the settings file in force would do on rows it never saw.
    holdout_at_settings = measure_thresholds(
        sealed, settings.t_hi, settings.t_lo, draws=draws,
        stratum_t_hi=(dict(settings.t_hi_by_stratum) or None),
    )
    # No dev split is not a licence to select on the sealed rows: the cuts below would then be
    # fitted and measured on the same pairs, and publishing them under "measured on the holdout"
    # would be false. The search still runs (its diagnostics are worth having) but every override
    # is refused, and the label says which rows it was taken over.
    by_stratum = stratum_thresholds(
        dev or scored, sealed,
        fitted_on="train+validation" if dev else "all judged (NO dev split; not held out)",
        allow_overrides=bool(dev),
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
                "labels_off_run": off_run[:20],
                "n_labels_off_run": len(off_run),
                "n_labels_off_run_judged": off_run_judged,
                "n_labels_off_run_positive": off_run_positive,
                "developer_project_suspected": developer_suspected,
                "weighted": weighted,
                "n_labelled_without_sample_weight": n_defaulted_weight,
            },
            "precedence": list(precedence) if precedence else None,
            "settings": {"t_hi": settings.t_hi, "t_lo": settings.t_lo,
                         "store_floor": settings.store_floor,
                         # What the engine RAN, beside `thresholds_by_stratum`, which is what the
                         # search RECOMMENDS. A reader must be able to tell the two apart.
                         "t_hi_by_stratum_in_force": dict(settings.t_hi_by_stratum),
                         "propose_only_strata_in_force": sorted(
                             key for key, value in settings.t_hi_by_stratum.items()
                             if value is None
                         )},
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
                # E25's dial is the share of stored pairs that actually LAND in the band — the
                # zone the engine decided, not a score interval. The two are different numbers:
                # an auto-rejected pair can score anywhere, and a certificate merges from inside
                # the interval, so `t_lo < score < t_hi` counts rows the queue never sees. Pairs
                # below `store_floor` are not stored and so are not in this denominator.
                "band_width": (n_band_zone / len(run_pairs)) if run_pairs else None,
                "n_band_zone": n_band_zone,
                "band_width_basis": "zone == band / stored pairs",
                # The score interval, kept under a name that says what it is.
                "score_in_band_share": (
                    sum(1 for score in population if settings.t_lo < score < settings.t_hi)
                    / len(population)
                ) if population else None,
                "weighted_sample_score_in_band_share": (
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
                "weight_positive_on_run": weight_positive_on_run,
                "weight_positive_off_run": weight_positive_total - weight_positive_on_run,
                "score_line_basis": "shares below divide by the ON-RUN positive weight: a pair the"
                                    " run never stored has no score to compare against T_lo",
                "weight_positive_below_t_lo": weight_positive_below_t_lo,
            "weight_positive_discarded": weight_positive_discarded,
            "positives_discarded_share": (
                weight_positive_discarded / weight_positive_on_run
                if weight_positive_on_run > 0 else None
            ),
            "weight_positive_gated_to_band": weight_positive_gated,
            "positives_gated_to_band_share": (
                weight_positive_gated / weight_positive_on_run
                if weight_positive_on_run > 0 else None
            ),
                "positives_below_t_lo_share": (
                    weight_positive_below_t_lo / weight_positive_on_run
                    if weight_positive_on_run > 0 else None
                ),
                "weight_positive_in_band": weight_positive_in_band,
                "positives_in_band_share": (
                    weight_positive_in_band / weight_positive_on_run
                    if weight_positive_on_run > 0 else None
                ),
                # Recall as the engine decided it, not as the score would have. The two lines
                # above are functions of `t_lo`/`t_hi` alone; these are functions of the whole
                # rule floor, and they are the ones a W4 verdict may quote.
                "zone_recall": {
                    "basis": "the zone decide_pair returned, gates and certificates included,"
                             " over EVERY judged positive — the unstored ones as `off_run`",
                    "weight_positive_by_zone": dict(sorted(weight_positive_by_zone.items())),
                    **{
                        f"positives_in_{name}_zone_share": (
                            weight_positive_by_zone.get(name, 0.0) / weight_positive_total
                            if weight_positive_total > 0 else None
                        )
                        for name in ("merge", "band", "reject", "veto", OFF_RUN_ZONE)
                    },
                },
            },
            "tiers": tier_rows,
            "fidelity": judge_fidelity(tiers),
            "thresholds": threshold_report.to_json(),
            "thresholds_by_stratum": by_stratum,
            "thresholds_all_judged": threshold_all.to_json(),
            "holdout": {
                "split_seed": seed,
                "n_dev": len(dev),
                "n_sealed": len(sealed),
                "fitted_on": (
                    "train+validation" if dev else "all judged (NO dev split; not held out)"
                ),
                "split_map_source": "fit" if split_map is not None else "re-derived from this run",
                "seal": seal,
                "expect_seal": expect_seal,
                **holdout,
            },
            "holdout_at_settings": {
                "n_sealed": len(sealed),
                "measured_on": "test (sealed)",
                "chosen_by": "the settings row in force, not the threshold search",
                "t_hi_by_stratum": dict(sorted(settings.t_hi_by_stratum.items())),
                **holdout_at_settings,
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


def components_spanning_split(
    run_pairs: Sequence[Mapping[str, Any]],
    groups: Mapping[int, int],
    seed: int = SPLIT_SEED,
    limit: int = 10,
) -> dict[str, Any]:
    """Whether a seal can ADJUDICATE this run: do its own merge components stay inside one split?

    E69. A seal is built from the components of the runs that existed when it was cut, so a
    CHALLENGER that merges pairs the incumbent banded produces components the map never saw. Each
    listing the map is missing falls back to its own id (`group_of`), which hashes into a split of
    its own — so one of the challenger's clusters can have members on both sides of the holdout,
    and a cluster-grain claim about it is then measured partly on rows the fit had seen. Measured
    on W9: g6 and honest_nofix, the two runs the fresh seal was unioned from, span nothing; the g7
    candidate spans 75 components over 231 listings, off 151 listings the map does not contain.

    This is a report, not a veto: the answer for a challenger that spans is either a seal cut from
    BLOCKING alone (every pair the engine could ever score, whatever it decides) or one unioned
    with the challenger's own edges before the challenger is measured."""
    components: dict[int, list[int]] = {}
    for listing, root in merge_groups(run_pairs).items():
        components.setdefault(int(root), []).append(int(listing))
    spanning: list[list[int]] = []
    listings, absent = 0, 0
    for members in components.values():
        if len(members) < 2:
            continue
        absent += sum(1 for item in members if item not in groups)
        if len({split_of(groups.get(item, item), seed) for item in members}) > 1:
            spanning.append(sorted(members))
            listings += len(members)
    return {
        "n_components": sum(1 for members in components.values() if len(members) > 1),
        "n_spanning": len(spanning),
        "listings_in_spanning": listings,
        "listings_absent_from_map": absent,
        "contained": not spanning,
        "sample": sorted(spanning, key=len, reverse=True)[:limit],
    }


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


def _fold_of(group: int, seed: int, folds: int) -> int:
    """Calibration folds are drawn by COMPONENT, like the split itself: two pairs of one cluster
    are not independent, and a calibration fold that straddles them reports its own training set."""
    digest = hashlib.blake2b(f"cal:{seed}:{group}".encode("utf-8"), digest_size=8).hexdigest()
    return int(digest, 16) % folds


def _mapped(
    fit_probs: Sequence[float],
    fit_ys: Sequence[int],
    eval_probs: Sequence[float],
    method: str,
    bins: int,
    fit_weights: Sequence[float] | None = None,
) -> list[float] | None:
    """`eval_probs` through a calibration map fitted on `fit_probs` — one scratch model, so the
    map is built and applied by exactly the code that will ship on the artifact."""
    scratch = LogisticModel(
        feature_order=(), weights={}, presence_weights={}, intercept=0.0
    )
    scratch.calibrate(
        list(fit_probs), list(fit_ys), bins=bins, method=method,
        weights=None if fit_weights is None else list(fit_weights),
    )
    if scratch.calibration is None:
        return None
    return [scratch.apply_calibration(prob) for prob in eval_probs]


def _cross_fit_ece(
    probs: Sequence[float],
    ys: Sequence[int],
    groups: Sequence[int],
    method: str,
    *,
    folds: int,
    seed: int,
    bins: int,
    weights: Sequence[float] | None = None,
) -> dict[str, Any]:
    """Out-of-fold ECE for one calibration map over the validation split.

    Weighted throughout when the draw has weights: the map is fitted on the design, scored on the
    design, and the selection criterion is the design-weighted ECE — because the precision target
    it feeds is a cohort rate, not a rate over the band-enriched rows the judge happened to see."""
    masses = [1.0] * len(probs) if weights is None else [float(value) for value in weights]
    assignment = [_fold_of(int(group), seed, folds) for group in groups]
    used = sorted(set(assignment))
    out_probs: list[float] = []
    out_ys: list[int] = []
    out_raw: list[float] = []
    out_weights: list[float] = []
    n_skipped = 0
    for fold in used:
        held = [index for index, value in enumerate(assignment) if value == fold]
        rest = [index for index, value in enumerate(assignment) if value != fold]
        if not held or not rest:
            n_skipped += len(held)
            continue
        mapped = _mapped(
            [probs[index] for index in rest], [ys[index] for index in rest],
            [probs[index] for index in held], method, bins,
            [masses[index] for index in rest],
        )
        if mapped is None:
            n_skipped += len(held)
            continue
        out_probs.extend(mapped)
        out_ys.extend(ys[index] for index in held)
        out_raw.extend(probs[index] for index in held)
        out_weights.extend(masses[index] for index in held)
    tail = [
        (prob, y, mass)
        for prob, y, raw, mass in zip(out_probs, out_ys, out_raw, out_weights)
        if raw >= MERGE_END_RAW
    ]
    whole = _mapped(probs, ys, [0.999999], method, bins, masses)
    tail_probs = [prob for prob, _, _ in tail]
    tail_ys = [y for _, y, _ in tail]
    tail_weights = [mass for _, _, mass in tail]
    tail_mass = sum(tail_weights)
    return {
        "method": method,
        "n_folds": len(used),
        "n_out_of_fold": len(out_probs),
        "n_skipped": n_skipped,
        "weighted": weights is not None,
        "ece": expected_calibration_error(
            out_probs, out_ys, bins, out_weights
        ) if out_probs else None,
        "ece_unweighted": expected_calibration_error(
            out_probs, out_ys, bins
        ) if out_probs else None,
        "log_loss": _log_loss(out_probs, out_ys) if out_probs else None,
        "n_merge_end": len(tail),
        # DIAGNOSTIC, never the selector: a map that returns one constant over the whole tail
        # scores ~0 here by construction, so choosing on it would reward refusing to discriminate
        # exactly where T_hi is read. The selector is the pooled out-of-fold ECE above; the
        # merge-end AUC beside it is the discrimination this number cannot see.
        "ece_merge_end": expected_calibration_error(
            tail_probs, tail_ys, bins, tail_weights
        ) if tail else None,
        "auc_merge_end": auc(tail_probs, tail_ys) if tail else None,
        "positive_rate_merge_end": (len(
            [y for y in tail_ys if y == 1]) / len(tail)) if tail else None,
        "positive_rate_merge_end_weighted": (
            sum(mass for _, y, mass in tail if y == 1) / tail_mass
        ) if tail_mass > 0 else None,
        # The highest probability this map can return at all, fitted on the WHOLE fold: a T_hi
        # above it is a threshold no pair can reach. It is a property of the MAP, not of the
        # model — a binned isotonic pools the top deciles and cannot exceed their joint rate.
        "ceiling": whole[0] if whole else None,
    }


def _calibrate_model(
    model: LogisticModel,
    validation: Sequence[Mapping[str, Any]],
    *,
    method: str = CALIBRATION_AUTO,
    folds: int = CALIBRATION_FOLDS,
    seed: int = SPLIT_SEED,
    bins: int = RELIABILITY_BINS,
) -> dict[str, Any]:
    """Pick the calibration map by out-of-fold validation ECE, then fit the winner on the fold.

    The number that decides is never the in-sample one: isotonic with a knot per bin can drive the
    in-sample ECE of any fold to near zero, which says nothing about the sealed test. `method` may
    also name one map outright, which is how a challenger is forced to a fixed shape."""
    model.calibration = None
    model.calibration_kind = None
    if not validation:
        return {"method": None, "reason": "no validation rows", "candidates": {}}
    raw = [model.predict_proba(row["feats"]) for row in validation]
    ys = [int(row["y"]) for row in validation]
    masses = [float(row.get("weight", 1.0)) for row in validation]
    pre = expected_calibration_error(raw, ys, bins, masses)
    names = list(CALIBRATION_METHODS) if method == CALIBRATION_AUTO else [method]
    if method != CALIBRATION_AUTO and method not in CALIBRATION_METHODS:
        raise ValueError(f"unknown calibration method {method!r}")
    groups = [int(row["group"]) for row in validation]
    candidates = {
        name: _cross_fit_ece(
            raw, ys, groups, name, folds=folds, seed=seed, bins=bins, weights=masses
        )
        for name in names
    }
    scored = [
        (body["ece"], name) for name, body in candidates.items() if body["ece"] is not None
    ]
    chosen = min(scored)[1] if scored else names[0]
    pre_again = model.calibrate(raw, ys, bins=bins, method=chosen, weights=masses)
    return {
        "method": chosen,
        "selected_by": (
            "out-of-fold validation ECE (design-weighted)" if len(names) > 1 else "caller"
        ),
        "candidates": candidates,
        "folds": folds,
        "weighted": True,
        "pre_calibration_ece_validation": pre_again if pre_again is not None else pre,
        "applied": model.calibration_kind,
        "ceiling": (candidates.get(chosen) or {}).get("ceiling"),
    }


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
    l2_grid: Sequence[float] | None = None,
    epochs: int = 3000,
    method: str = "irls",
    max_iter: int = FIT_MAX_ITER,
    tol: float | None = None,
    version: str | None = None,
    split_map: Mapping[int, int] | None = None,
    calibration: str = CALIBRATION_AUTO,
    calibration_folds: int = CALIBRATION_FOLDS,
) -> tuple[LogisticModel, FitReport]:
    """Fit the §6 scorer on judged pairs, split 60/20/20 by component (never by pair).

    `method` picks the optimiser: `irls` (the default) takes Newton steps and lands on the
    penalised MLE in a handful of iterations; `gd` is the original full-batch gradient descent,
    kept because `--epochs` is how a deliberately under-fit model is produced.

    Training weights ARE label credibility x design weight — a gold row should outvote a text
    row, and a thin stratum stands for more of the cohort. That product is capped at a multiple
    of the median weight rather than at a constant, so the cap tames the tail without re-ordering
    the design, and the realised design effect is reported beside it."""
    if split != "cluster":
        raise ValueError(f"only the cluster split is implemented, got {split!r}")
    if method not in FIT_METHODS:
        raise ValueError(f"unknown fit method {method!r}, expected one of {sorted(FIT_METHODS)}")
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
    feats_train = [row["feats"] for row in train]
    ys_train = [row["y"] for row in train]
    masses_train = [row["weight"] for row in train]

    def fitted(penalty: float) -> tuple[LogisticModel, dict[str, Any]]:
        candidate = hand_initialised()
        if method == "irls":
            report = candidate.fit_irls(
                feats_train, ys_train, l2=penalty, max_iter=max_iter,
                tol=IRLS_TOLERANCE if tol is None else tol, sample_weights=masses_train,
            )
        else:
            candidate.fit(
                feats_train, ys_train, l2=penalty, epochs=epochs,
                tol=FIT_TOLERANCE if tol is None else tol, sample_weights=masses_train,
            )
            report = {}
        return candidate, report

    # The sweep is scored on the validation split, UNCALIBRATED (`fit_irls` leaves no knots): a
    # calibration map fitted on the same fold would absorb part of the penalty's effect and flatten
    # the grid into noise. Ties go to the STIFFER penalty — same fit, fewer effective parameters.
    grid = sorted({float(value) for value in l2_grid}) if l2_grid else []
    grid_rows: list[dict[str, Any]] = []
    chosen_l2 = float(l2)
    selected_on = "caller"
    if grid:
        selected_on = "validation" if validation else "train"
        best: tuple[float, float] | None = None
        for penalty in grid:
            candidate, _ = fitted(penalty)
            train_probs = [candidate.predict_proba(row["feats"]) for row in train]
            scored_rows = validation or train
            probs = [candidate.predict_proba(row["feats"]) for row in scored_rows]
            ys = [int(row["y"]) for row in scored_rows]
            loss = _log_loss(probs, ys)
            grid_rows.append({
                "l2": penalty,
                "in_task_grid": penalty in L2_TASK_GRID,
                "train_log_loss": _log_loss(train_probs, [int(row["y"]) for row in train]),
                "selection_log_loss": loss,
                "selection_auc": auc(probs, ys),
                "selection_ece": expected_calibration_error(probs, ys),
                "converged": bool(candidate.fit_report.get("converged")),
            })
            if best is None or (loss, -penalty) < (best[0], -best[1]):
                best = (loss, penalty)
        chosen_l2 = best[1] if best is not None else chosen_l2

    model, design_report = fitted(chosen_l2)
    calibration_report = _calibrate_model(
        model, validation, method=calibration, folds=calibration_folds, seed=seed,
    )
    pre_ece = calibration_report.get("pre_calibration_ece_validation")

    def measured(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"n": 0}
        probs = [model.predict_proba(row["feats"]) for row in rows]
        ys = [int(row["y"]) for row in rows]
        masses = [float(row.get("weight", 1.0)) for row in rows]
        prior_probs = [prior.predict_proba(row["feats"]) for row in rows]
        return {
            "n": len(rows),
            "ece_weighted": expected_calibration_error(probs, ys, RELIABILITY_BINS, masses),
            "ece_prior_weighted": expected_calibration_error(
                prior_probs, ys, RELIABILITY_BINS, masses
            ),
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
    # The optimiser is part of the provenance: two methods on one split are two different
    # coefficient vectors, and `model_version` is what reaches the scored rows in the DB.
    model.version = version or f"fit_{method}_{len(train)}_{seed}"
    # Stamped on the dataclass, so `to_json` carries it and `from_json` can check the feature
    # digest: a model fitted before a feature lands scores a different function than the one
    # measured here, and that has to fail at load rather than drift.
    model.provenance = {
        "feature_version": {
            "sha256_16": model.feature_digest(), "n_features": len(model.feature_order),
        },
        "seal": split_seal(groups),
        "training": {
            "method": method, "l2": chosen_l2, "seed": seed, "split": split,
            "n_train": len(train), "n_validation": len(validation), "n_test": len(test),
            "calibration": calibration_report.get("method"),
            "weight_cap": cap,
        },
    }

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
                # The validation ECE inside `validation_metrics` is measured AFTER the knots were
                # fitted on that same split: in-sample, and not the E21 gate. The honest numbers
                # are the pre-fit validation ECE, the OUT-OF-FOLD ECE the map was chosen on, and
                # the sealed test ECE.
                "method": calibration_report.get("method"),
                "selected_by": calibration_report.get("selected_by"),
                "candidates": calibration_report.get("candidates", {}),
                # The highest probability the CHOSEN map can return: a T_hi above it is a cut no
                # pair can reach, and that is a fact about the map, not about the model.
                "ceiling": calibration_report.get("ceiling"),
                "weighted": calibration_report.get("weighted"),
                "folds": calibration_report.get("folds"),
                "pre_calibration_ece_validation": pre_ece,
                "post_calibration_ece_validation_in_sample": validation_metrics.get("ece"),
                "test_ece": test_metrics.get("ece"),
                "ece_gate": 0.05,
                "ece_gate_ok": (
                    test_metrics["ece"] <= 0.05 if test_metrics.get("n") else None
                ),
                "knots": [[edge, value] for edge, value in (model.calibration or [])],
            },
            "l2_grid": {
                "grid": grid,
                "task_grid": list(L2_TASK_GRID),
                "rows": grid_rows,
                "chosen": chosen_l2,
                "selected_on": selected_on,
                "criterion": "log loss, uncalibrated; ties to the stiffer penalty",
                # A penalty at either end of the grid is not a minimum, it is where the search
                # ran out of room — say so rather than leaving a reader to compare the rows.
                "chosen_is_edge": bool(grid) and chosen_l2 in (min(grid), max(grid)),
            },
            "convergence": {
                **dict(model.fit_report), "converged_flag": converged, "method": method,
                "l2": chosen_l2, "tol": (IRLS_TOLERANCE if method == "irls" else FIT_TOLERANCE)
                if tol is None else tol,
                # Only the ceiling that APPLIED: `epochs` beside a 10-step Newton fit reads as
                # evidence about a knob that was never consulted.
                **({"max_iter": max_iter} if method == "irls" else {"epochs": epochs}),
            },
            "design": {
                "reported": method == "irls",
                "n_terms": design_report.get("terms"),
                "n_identified": design_report.get("terms_identified"),
                "dropped_terms": design_report.get("dropped_terms", []),
                "duplicate_groups": design_report.get("duplicate_groups", []),
                "pinned_terms": design_report.get("pinned_terms", []),
                "gradient_fallbacks": design_report.get("gradient_fallbacks", 0),
            },
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
                 f"{_pct(sections['band_composition']['band_width'])}"
                 f" of {sections['band_composition']['n_population']} stored pairs land in the"
                 f" band (E25; score interval "
                 f"{_pct(sections['band_composition']['score_in_band_share'])})")
    lines.append(f"reject zone           judged {reject['n_judged']}  positive share "
                 f"{_pct(reject['rate'])}  HT-weighted {_pct(reject['weighted_rate'])}")
    lines.append(f"missed positives      below T_lo={sections['settings']['t_lo']:.4g}: "
                 f"{_pct(cohort['positives_below_t_lo_share'])} of weighted positives"
                 f"   in band: {_pct(cohort['positives_in_band_share'])}"
                 f"   [SCORE-based: blind to the gates and the certificates]" + marker)
    zone_recall = cohort["zone_recall"]
    lines.append(f"zone recall           merge "
                 f"{_pct(zone_recall['positives_in_merge_zone_share'])} of weighted positives"
                 f"   band {_pct(zone_recall['positives_in_band_zone_share'])}"
                 f"   reject {_pct(zone_recall['positives_in_reject_zone_share'])}"
                 f"   veto {_pct(zone_recall['positives_in_veto_zone_share'])}"
                 f"   off-run {_pct(zone_recall['positives_in_off_run_zone_share'])}"
                 f"   [ZONE-based: what the engine actually did]" + marker)
    merge_band = (zone_recall["positives_in_merge_zone_share"] or 0.0) + (
        zone_recall["positives_in_band_zone_share"] or 0.0
    )
    lines.append(
        f"                      merge+band {_pct(merge_band)} of ALL judged positives"
        f" ({counts['n_labels_off_run_positive']} judged positives are off-run:"
        f" the run never stored them, so no threshold can reach them)"
    )
    t_hi = thresholds_json["t_hi"]
    lines.append(
        f"thresholds (dev)      T_hi {'unattainable' if t_hi is None else f'{t_hi:.12g}'}"
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
        f"   [the SEARCHED cut, not the shipped table]"
    )
    in_force = sections["holdout_at_settings"]
    propose_only = sorted(
        key for key, value in (in_force.get("t_hi_by_stratum") or {}).items() if value is None
    )
    lines.append(
        f"holdout at the settings in force   n {in_force['n_sealed']}"
        f"  merge set {in_force.get('n_merge', 0)}"
        f"  precision {_pct(in_force.get('precision'))}"
        f"  CP LB {_pct(in_force.get('clopper_pearson_lb'))}"
        f"  min cell {_pct(in_force.get('min_stratum_precision'))}"
        f"  floor ok {in_force.get('stratum_floor_ok')}"
        f"   [E48 table applied; propose-only: {', '.join(propose_only) or 'none'}]"
    )
    fidelity = sections["fidelity"]["pooled"]
    lines.append(f"judge fidelity        cheap-vs-gold {_pct(fidelity['agreement'])}"
                 f" on n={fidelity['n']}  (LB {_pct(fidelity['wilson_lb'])})")
    side = sections["agreement"]["by_side"]
    parts = [f"{name} {_pct(body['agreement'])} (n={body['n']})" for name, body in side.items()]
    lines.append(f"judge-vs-engine       {'   '.join(parts) or '-'}")
    nonresponse = sections["sample"]["nonresponse"]
    design = sections["sample"]["design"]
    conflicts = sections["sample"].get("frame_conflicts") or []
    if conflicts:
        lines.append(
            f"frame conflicts       {len(conflicts)} strata whose newest frame cannot hold the"
            f" pairs drawn in it — population taken from the widest frame that named them:"
        )
        for body in conflicts[:6]:
            lines.append(
                f"                        {body['stratum']}  drawn {body['n_selected_pooled']}"
                f"  newest frame {body['population_newest_frame']}"
                f"  used {body['population_used']}"
                f"  weight {body['weight_used']:.2f}"
                f" (newest frame would give {body['weight_under_newest_frame']:.2f})"
            )
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
    by_stratum = sections.get("thresholds_by_stratum") or {}
    if by_stratum:
        lines.extend([
            "", f"## Per-stratum thresholds (fitted on {by_stratum['fitted_on']}, measured on "
            f"{by_stratum['measured_on']})", "",
            f"key `{by_stratum['key']}`   gate LB >= "
            f"{by_stratum['criteria']['precision_lb']:.2f} and point >= "
            f"{by_stratum['criteria']['precision_point']:.3f}"
            f"   a perfect stratum still needs {by_stratum['labels_needed_for_lb']} judged merges "
            "to clear that lower bound", "",
            "| stratum | dev n | T_hi | n at cut | precision | wilson LB | relaxed cut "
            "| n | precision | wilson LB | cert merges | cert precision | sealed n "
            "| sealed precision | sealed LB | ships |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: "
            "| ---: | ---: | ---: | ---: | --- |",
        ])
        for name, body in by_stratum["strata"].items():
            verdict = "auto-merge" if not body["propose_only"] else (
                f"propose-only ({body['propose_only_reason']})"
            )
            # 12 significant digits, not 4: two cuts of a saturated calibration map differ in
            # the 9th decimal and print identically at 4, and the rounded value names a merge set
            # ~60x larger than the one measured. A published cut has to be re-enterable.
            cut = "-" if body["t_hi"] is None else f"{body['t_hi']:.12g}"
            relaxed = "-" if body["relaxed_t_hi"] is None else f"{body['relaxed_t_hi']:.12g}"
            lines.append(
                f"| {name} | {body['n_dev']} "
                f"| {cut} "
                f"| {body['n_at_t_hi']} | {_pct(body['precision_at_t_hi'])} "
                f"| {_pct(body['wilson_lb_at_t_hi'])} "
                f"| {relaxed} "
                f"| {body['n_at_relaxed']} | {_pct(body['precision_at_relaxed'])} "
                f"| {_pct(body['wilson_lb_at_relaxed'])} "
                f"| {body['n_certificate_merges']} "
                f"| {_pct(body['precision_certificate_merges'])} "
                f"| {body['sealed_n_merge']} | {_pct(body['sealed_precision'])} "
                f"| {_pct(body['sealed_wilson_lb'])} | {verdict} |"
            )
        lines.append("")
        lines.append(f"auto-merge strata: {by_stratum['auto_merge_strata'] or 'none'}   "
                     f"propose-only: {by_stratum['propose_only'] or 'none'}")
    lines.extend(["", "## Thresholds (fitted on train+validation)", "", "```",
                  json.dumps(sections["thresholds"], indent=2, sort_keys=True), "```", "",
                  "## Thresholds over every judged pair (cross-check, selection-inflated)", "",
                  "```",
                  json.dumps(sections["thresholds_all_judged"], indent=2, sort_keys=True),
                  "```", "",
                  "## Sealed holdout re-measurement (at the SEARCHED thresholds)", "", "```",
                  json.dumps(sections["holdout"], indent=2, sort_keys=True), "```", "",
                  "## Sealed holdout at the settings row actually loaded", "", "```",
                  json.dumps(sections["holdout_at_settings"], indent=2, sort_keys=True),
                  "```", "",
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
        f"calibration E21       map {calibration.get('method') or 'none'}"
        f" ({calibration.get('selected_by') or '-'})"
        f"   validation ECE pre-fit "
        f"{_pct(calibration['pre_calibration_ece_validation'])}"
        f"   sealed test ECE {_pct(calibration['test_ece'])}"
        f"   gate <= 5.00%: {calibration['ece_gate_ok']}"
    )
    for name, body in sorted((calibration.get("candidates") or {}).items()):
        lines.append(
            f"  out-of-fold           {name:<13} ECE {_pct(body.get('ece'))}"
            f" (unweighted {_pct(body.get('ece_unweighted'))})"
            # The merge-end numbers are DIAGNOSTIC: a constant map scores ~0 ECE there by
            # refusing to discriminate, which the AUC beside it is what catches.
            f"   merge end: ECE {_pct(body.get('ece_merge_end'))}"
            f" AUC {_pct(body.get('auc_merge_end'))} (n {body.get('n_merge_end')})"
            f"   ceiling {_pct(body.get('ceiling'))}"
            f"   n {body.get('n_out_of_fold')} over {body.get('n_folds')} folds"
        )
    sweep = sections.get("l2_grid") or {}
    if sweep.get("rows"):
        lines.append(
            f"l2 grid               chosen {sweep['chosen']:g} on {sweep['selected_on']}"
            f"  ({sweep['criterion']})"
        )
        for row in sweep["rows"]:
            mark = " <-" if row["l2"] == sweep["chosen"] else ""
            lines.append(
                f"  l2={row['l2']:<8g}        log loss {row['selection_log_loss']:.4f}"
                f"   AUC {row['selection_auc']:.4f}   ECE {row['selection_ece']:.4f}"
                f"   train log loss {row['train_log_loss']:.4f}{mark}"
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
    method = convergence.get("method", "gd")
    if method == "irls":
        # More iterations do not rescue an unidentified design: with l2 at 0 over columns that
        # duplicate the intercept the penalised MLE is at infinity, and RAISING l2 is the knob.
        steps, residual, knob = (
            convergence.get("iterations"),
            f"max|delta| {convergence.get('max_abs_delta')}",
            "--max-iter or raise --l2",
        )
    else:
        steps, residual, knob = (
            convergence.get("epochs_run"),
            f"mean|grad| {convergence.get('mean_abs_grad')}",
            "--epochs",
        )
    if not convergence.get("converged_flag"):
        lines.append(
            f"NOT CONVERGED         {method}: {steps} iterations at l2={convergence['l2']},"
            f" {residual}"
            f" — raise {knob} before reading a threshold off this model"
        )
    else:
        lines.append(f"converged             {json.dumps(convergence, sort_keys=True)}")
    design = sections.get("design") or {}
    if design.get("reported"):
        dropped = design.get("dropped_terms") or []
        groups = design.get("duplicate_groups") or []
        shown = ", ".join(dropped[:4]) + (f", +{len(dropped) - 4} more" if len(dropped) > 4 else "")
        lines.append(
            f"design                {design.get('n_terms')} terms ->"
            f" {design.get('n_identified')} identified"
            f"   dropped {len(dropped)} constant{f' ({shown})' if dropped else ''}"
        )
        for members in groups[:5]:
            lines.append(f"  collinear             {' = '.join(members)}")
        if len(groups) > 5:
            lines.append(f"  collinear             +{len(groups) - 5} more groups")
    lines.append("top learned weights")
    for entry in sections["weights"][:10]:
        lines.append(f"  {entry['feature']:<26}{entry['weight']: .4f}")
    return lines


def _fit_markdown(report: FitReport) -> str:
    sections = report.sections
    lines = [f"# {report.title}", "", "## Headline", "", "```"]
    lines.extend(report.headline())
    lines.extend(["```", "", "## Split seal", "", "```",
                  json.dumps(sections["split"], indent=2, sort_keys=True), "```"])
    design = sections.get("design") or {}
    if design.get("reported"):
        lines.extend(["", "## Design (identified terms)", "", "```",
                      json.dumps(design, indent=2, sort_keys=True), "```"])
    lines.extend(["", "## Learned weights", "", "| feature | weight | presence weight |",
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
