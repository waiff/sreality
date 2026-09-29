"""One run read against the rulings (SW1, ADD07), and the refit (§6).

**`measure` / `read_lists`.** A run is decided blind and the rulings are the test set: M1 pair
precision with its Wilson lower bound, M2 recall, M3 group purity, M4 attribution, M5 coverage,
and against a base run the D83 read lists — every pair, merge edge, co-pair and group gained or
lost, each co-pair tagged with its ruling and its truth16 triage class (K40). Nothing here
decides a pair; `harness evaluate` decides the ruled pairs a run never stored, through the lane's
own `decide_pair`, before this reads them.

**`fit_model`.** 60/20/20 **by component, never by pair** — pairs inside one cluster are not
independent — over a group map that is stamped and re-usable, so a challenger model can be scored
on the incumbent's seal. Training weights are label credibility, capped at a multiple of the
median with the realised design effect reported.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from autodedup.labels import Label, PairKey, pair_key
from autodedup.model import (
    CALIBRATION_METHODS,
    FIT_TOLERANCE,
    IRLS_TOLERANCE,
    LogisticModel,
    auc,
    expected_calibration_error,
    hand_initialised,
)

Z_95: float = 1.96

MERGE_ZONE: str = "merge"


# A stratum weight is a population ratio and the real draw runs to ~500x on the thinnest cell.
# Capping at a constant would re-order the design (a 4,000-pair stratum and an 80-pair one would
# land within a factor of two), so the cap is a MULTIPLE OF THE MEDIAN weight and the realised
# design effect is reported next to it — the cap is visible, never silent.
WEIGHT_CAP_MEDIAN_MULTIPLE: float = 10.0


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


# --- one run against one rulings file: M1-M5 and the D83 read lists (SW1, ADD07) -----------------
#
# A run is decided blind; the rulings are the test set. Withholding is by input, never by flag.

RULINGS_FILES: tuple[str, ...] = ("operator_labels.jsonl", "operator_merges.jsonl",
                                  "must_not_link.jsonl")
SAME: str = "same"
DIFFERENT: str = "different"
UNRULED: str = "unruled"
# D3: below this many held-out ruled pairs no precision bound clears 0.99, so none is printed.
MEASURABLE_N: int = 380


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def read_rulings(path: str | Path) -> dict[PairKey, str]:
    """The NEWEST ruling per pair, `same` or `different`, off the labels lane's export directory:
    a pair verdict, every member pair of a Browse merge, and a must-not-link. `unsure` is no
    statement at all."""
    root = Path(path)
    events: list[tuple[str, PairKey, str]] = []
    for row in _jsonl(root / RULINGS_FILES[0]):
        verdict = str(row.get("verdict") or "")
        if verdict and verdict != "unsure":
            events.append((str(row.get("decided_at") or ""),
                           pair_key(row["listing_lo"], row["listing_hi"]),
                           SAME if verdict == SAME else DIFFERENT))
    for row in _jsonl(root / RULINGS_FILES[1]):
        members = sorted({int(member["listing_id"]) for member in row.get("members") or ()})
        stamp = str(row.get("merged_at") or "")
        events += [(stamp, (a, b), SAME)
                   for i, a in enumerate(members) for b in members[i + 1:]]
    for row in _jsonl(root / RULINGS_FILES[2]):
        events.append((str(row.get("created_at") or ""),
                       pair_key(row["listing_lo"], row["listing_hi"]), DIFFERENT))
    rulings: dict[PairKey, str] = {}
    for _stamp, key, verdict in sorted(events):
        rulings[key] = verdict
    return rulings


def read_reference(path: str | Path | None) -> dict[PairKey, str]:
    """truth16's label-free reference, a TRIAGE input (K40): `CD` certain duplicates, `CN`
    structural negatives. Never a bar."""
    if not path:
        return {}
    root = Path(path)
    out = {pair_key(row["lo"], row["hi"]): "CD"
           for row in _jsonl(root / "certain_duplicates.jsonl")}
    out.update({pair_key(row["lo"], row["hi"]): "CN"
                for row in _jsonl(root / "structural_labels.jsonl")
                if row.get("label") == DIFFERENT})
    return out


@dataclass(frozen=True, slots=True)
class RunView:
    """What `harness run` wrote: the stored rows by pair, and the groups."""

    rows: dict[PairKey, dict[str, Any]]
    groups: dict[int, tuple[int, ...]]

    @property
    def group_of(self) -> dict[int, int]:
        return {i: key for key, members in self.groups.items() for i in members}

    def co_pairs(self) -> set[PairKey]:
        return {(a, b) for members in self.groups.values()
                for i, a in enumerate(members) for b in members[i + 1:]}


def read_run(run_dir: str | Path) -> RunView:
    root = Path(run_dir)
    rows: dict[PairKey, dict[str, Any]] = {}
    with gzip.open(root / "pairs.jsonl.gz", "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                rows[pair_key(row["lo"], row["hi"])] = row
    clusters = json.loads((root / "clusters.json").read_text(encoding="utf-8"))["clusters"]
    return RunView(rows, {int(key): tuple(sorted(int(i) for i in members))
                          for key, members in clusters.items()})


def deciding_layer(row: Mapping[str, Any] | None) -> str:
    """Which layer decided a row, off its columns alone (no reason string is parsed): the wall
    or veto that refused it, the proof that certified it, or the score. SW5a's `rung` replaces it."""
    if row is None:
        return "not_decided"
    if row.get("veto"):
        return f"veto:{row['veto']}"
    if row.get("certificate"):
        return f"proof:{row['certificate']}"
    return "score"


def attribution(rows: Iterable[Mapping[str, Any] | None]) -> dict[str, dict[str, int]]:
    """M4: `zone|layer` x evidence family, one count per row and family (`NONE` when a row
    carries no family). The one attribution function: it reads decisions, never re-makes one."""
    table: dict[str, dict[str, int]] = {}
    for row in rows:
        zone = str(row.get("zone")) if row is not None else "none"
        cells = table.setdefault(f"{zone}|{deciding_layer(row)}", {"n": 0})
        cells["n"] += 1
        for family in (row or {}).get("families") or ["NONE"]:
            cells[family] = cells.get(family, 0) + 1
    return dict(sorted(table.items()))


def measure(run: RunView, rulings: Mapping[PairKey, str], ids: set[int],
            explicit: Mapping[PairKey, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """M1 pair precision (Wilson lower bound), M2 recall, M3 group purity, M4 attribution over
    every co-clustered and every ruled pair, M5 coverage. `explicit` holds the ruled pairs the
    run never stored, decided as an explicit input set (never grouped)."""
    ruled = {key: verdict for key, verdict in rulings.items()
             if key[0] in ids and key[1] in ids}
    co = run.co_pairs()
    together = {key: verdict for key, verdict in ruled.items() if key in co}
    k_diff = sum(1 for verdict in together.values() if verdict == DIFFERENT)
    n_same = sum(1 for verdict in ruled.values() if verdict == SAME)
    n_diff = len(ruled) - n_same
    group_of = run.group_of
    impure = {group_of[key[0]] for key, verdict in ruled.items()
              if verdict == DIFFERENT and key in co}
    lookup = {**(explicit or {}), **run.rows}
    measurable = len(ruled) >= MEASURABLE_N
    return {
        "M1_precision": {
            "co_clustered_ruled": len(together), "ruled_different_together": k_diff,
            "precision": (1 - k_diff / len(together)) if together else None,
            "wilson_lower": (wilson_lower(len(together) - k_diff, len(together))
                             if together and measurable else None)},
        "M2_recall": {"ruled_same": n_same,
                      "together": len(together) - k_diff,
                      "recall": ((len(together) - k_diff) / n_same) if n_same else None},
        "M3_purity": {"groups": len(run.groups), "with_a_ruled_different_pair": len(impure),
                      "purity": (1 - len(impure) / len(run.groups)) if run.groups else None},
        "M4_attribution": {
            "co_clustered": attribution(run.rows.get(key) for key in sorted(co)),
            "ruled": {verdict: attribution(lookup.get(key) for key, v in sorted(ruled.items())
                                           if v == verdict) for verdict in (SAME, DIFFERENT)}},
        "M5_coverage": {"ruled": len(ruled), "same": n_same, "different": n_diff,
                        "measurable": measurable,
                        "verdict": "measurable" if measurable else
                        f"not measurable (n={len(ruled)} < {MEASURABLE_N})"},
    }


def read_lists(base: RunView, arm: RunView, rulings: Mapping[PairKey, str],
               reference: Mapping[PairKey, str] | None = None) -> dict[str, Any]:
    """The D83 read lists: every pair, merge edge, co-pair and group the arm gains or loses
    against the base, each co-pair tagged with its ruling and its truth16 triage class."""
    reference = reference or {}

    def tagged(keys: Iterable[PairKey]) -> list[list[Any]]:
        return [[lo, hi, rulings.get((lo, hi), UNRULED), reference.get((lo, hi))]
                for lo, hi in sorted(keys)]

    merges = lambda view: {k for k, r in view.rows.items() if r.get("zone") == "merge"}  # noqa: E731
    changed = sorted(k for k in set(base.rows) & set(arm.rows)
                     if base.rows[k].get("zone") != arm.rows[k].get("zone"))
    moves: dict[str, int] = {}
    for key in changed:
        move = f"{base.rows[key].get('zone')}->{arm.rows[key].get('zone')}"
        moves[move] = moves.get(move, 0) + 1
    co_base, co_arm = base.co_pairs(), arm.co_pairs()
    groups_base, groups_arm = set(base.groups.values()), set(arm.groups.values())
    lists = {
        "pairs_new": [list(k) for k in sorted(set(arm.rows) - set(base.rows))],
        "pairs_lost": [list(k) for k in sorted(set(base.rows) - set(arm.rows))],
        "zone_changed": [[lo, hi, base.rows[(lo, hi)].get("zone"), arm.rows[(lo, hi)].get("zone")]
                         for lo, hi in changed],
        "merge_gained": [list(k) for k in sorted(merges(arm) - merges(base))],
        "merge_lost": [list(k) for k in sorted(merges(base) - merges(arm))],
        "copairs_gained": tagged(co_arm - co_base),
        "copairs_lost": tagged(co_base - co_arm),
        "groups_arm_only": [list(g) for g in sorted(groups_arm - groups_base)],
        "groups_base_only": [list(g) for g in sorted(groups_base - groups_arm)],
    }
    counts = {name: len(items) for name, items in lists.items()}
    counts["zone_moves"] = dict(sorted(moves.items()))
    for name in ("copairs_gained", "copairs_lost"):
        counts[f"{name}_by_ruling"] = _count(row[2] for row in lists[name])
        counts[f"{name}_by_reference"] = _count(row[3] or "-" for row in lists[name])
    return {"counts": counts, "lists": lists}


def _count(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


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


def _feats(row: Mapping[str, Any]) -> dict[str, tuple[float, bool]]:
    out: dict[str, tuple[float, bool]] = {}
    for name, entry in (row.get("feats") or {}).items():
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            out[str(name)] = (float(entry[0]), bool(entry[1]))
    return out


def _pct(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value * 100:.{digits}f}%"


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

    Training weights ARE label credibility — a gold row should outvote a text row — capped at a
    multiple of the median weight rather than at a constant, so the cap tames the tail without
    re-ordering the labels, and the realised design effect is reported beside it."""
    if split != "cluster":
        raise ValueError(f"only the cluster split is implemented, got {split!r}")
    if method not in FIT_METHODS:
        raise ValueError(f"unknown fit method {method!r}, expected one of {sorted(FIT_METHODS)}")
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
            "weight": label.weight,
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
