"""W15 census C2: one in-memory engine pass whose features are computed ONCE, then re-decided and
re-clustered per ablation arm. Offline, no DB. Every arm is compared with this module's own FULL
run, which must reproduce the stored generation's pair rows exactly (verified by `verify`)."""
from __future__ import annotations

import copy
import gzip
import json
import os
import pickle
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

WT = os.environ.get("WT", "/home/hejtm/dev/sreality/.claude/worktrees/w15-census-c2")
sys.path.insert(0, WT)

import numpy as np  # noqa: E402

from autodedup import d43 as d43_mod  # noqa: E402
from autodedup import decide as decide_mod  # noqa: E402
from autodedup import indistinguishable as ind_mod  # noqa: E402
from autodedup import yardstick as yard_mod  # noqa: E402
from autodedup.blocking import generate_pairs  # noqa: E402
from autodedup.cluster import cluster_pairs  # noqa: E402
from autodedup.d43 import relation_for  # noqa: E402
from autodedup.dataset import load  # noqa: E402
from autodedup.decide import (  # noqa: E402
    Decision, _decide_layers, apply_context_rule, apply_d43_rule, apply_merge_policy,
    stratum_t_hi,
)
from autodedup.features import (  # noqa: E402
    ABSENT, FAMILY_OF, FEATURE_ORDER, FeatureContext, pair_features,
)
from autodedup.fingerprint import build_all  # noqa: E402
from autodedup.guards import UNIT_DESIGNATOR_VETO  # noqa: E402
from autodedup.harness import load_model, load_must_not_link  # noqa: E402
from autodedup.hazard_context import ContextIndex  # noqa: E402
from autodedup.indistinguishable import FEATURE_SLOTS  # noqa: E402
from autodedup.labels import pair_key  # noqa: E402
from autodedup.model import CALIBRATION_TIE_BREAK, LogisticModel  # noqa: E402
from autodedup.settings import Settings  # noqa: E402
from autodedup.store_score import storable  # noqa: E402

ART = Path("/home/hejtm/autodedup-artifacts")
LABELS = ART / "w14/labels_g13_36225845749/autodedup-labels-36225845749"
COHORTS = {
    "trial": ART / "w14/s15/score_g15/autodedup-score-36244048665/artifact/cohort.jsonl.gz",
    "c17": ART / "w14/s15/cohort17_export_36221961445/autodedup-export-36221961445/cohort.jsonl.gz",
    "c18": ART / "w14/s15/cohort18_export_36237638871/autodedup-export-36237638871/cohort.jsonl.gz",
}
SETTINGS = Path(WT) / "autodedup/settings/w31.json"
MODEL = Path(WT) / "autodedup/models/w6_gold.json"
F = len(FEATURE_ORDER)
FIDX = {name: i for i, name in enumerate(FEATURE_ORDER)}

# --- the fact-reader census hook ----------------------------------------------------------
_ORIGINAL_DF = ind_mod.distinguishing_facts
DISABLED: set[str] = set()
PHASE: list[str] = ["decide"]
LOG: list[tuple[str, str, tuple[int, int], tuple[str, ...]]] | None = None


def _hooked(a, b, feats=None, settings=None, mode=ind_mod.PROMOTE):
    facts = _ORIGINAL_DF(a, b, feats, settings, mode)
    if LOG is not None:
        LOG.append((PHASE[0], mode, pair_key(a.id, b.id), tuple(f.name for f in facts)))
    if DISABLED:
        facts = [f for f in facts if f.name not in DISABLED]
    return facts


for _module in (ind_mod, decide_mod, d43_mod, yard_mod):
    _module.distinguishing_facts = _hooked


def feats_of(V: np.ndarray, P: np.ndarray, i: int) -> dict[str, tuple[float, bool]]:
    return {name: ((float(V[i, j]), True) if P[i, j] else ABSENT)
            for j, name in enumerate(FEATURE_ORDER)}


@dataclass
class Run:
    decisions: list[Decision]
    clusters: dict[int, list[int]]
    member_of: dict[int, int]
    conflicts: list[dict]
    stats: dict
    seconds: float = 0.0


@dataclass
class Engine:
    name: str
    ds: Any
    settings: Settings
    model: LogisticModel
    fps: dict
    keys: list[tuple[int, int]]
    probes: list[frozenset]
    V: np.ndarray
    P: np.ndarray
    hazard: Any
    mnl: frozenset = field(default_factory=frozenset)
    base: Run | None = None
    pre: list[Decision] | None = None

    # ------------------------------------------------------------------ building
    @classmethod
    def build(cls, name: str, cache_dir: Path, mnl: bool = False) -> "Engine":
        clock = time.perf_counter()
        ds = load(str(COHORTS[name]))
        settings = Settings.from_json(SETTINGS)
        model = load_model(str(MODEL))
        fps = build_all(ds, settings)
        hazard = ContextIndex.build(ds.listings, ds.images_by_listing)
        cache = cache_dir / f"feats_{name}.pkl"
        if cache.is_file():
            with open(cache, "rb") as handle:
                keys, probes, V, P = pickle.load(handle)
            print(f"[{name}] features from cache {cache} ({len(keys)} pairs)", flush=True)
        else:
            pairs, _ = generate_pairs(fps, settings)
            ctx = FeatureContext.build(fps, settings, ds)
            ctx.index_attrs(fps, ds.listings)
            keys = sorted(pairs)
            probes = [frozenset(pairs[k]) for k in keys]
            V = np.zeros((len(keys), F), dtype=np.float64)
            P = np.zeros((len(keys), F), dtype=bool)
            for i, (lo, hi) in enumerate(keys):
                feats = pair_features(fps[lo], fps[hi], ds.listings[lo], ds.listings[hi],
                                      ds.images(lo), ds.images(hi), ctx, settings)
                for j, fname in enumerate(FEATURE_ORDER):
                    value, present = feats.get(fname, ABSENT)
                    V[i, j] = float(value)
                    P[i, j] = bool(present)
                if i % 20000 == 0:
                    print(f"[{name}] features {i}/{len(keys)} {time.perf_counter()-clock:.0f}s",
                          flush=True)
            cache_dir.mkdir(parents=True, exist_ok=True)
            with open(cache, "wb") as handle:
                pickle.dump((keys, probes, V, P), handle, protocol=5)
        ids = set(ds.listings)
        must_not = frozenset()
        if mnl:
            must_not = frozenset(p for p in load_must_not_link(str(LABELS / "must_not_link.jsonl"))
                                 if p[0] in ids and p[1] in ids)
        eng = cls(name, ds, settings, model, fps, keys, probes, V, P, hazard, must_not)
        print(f"[{name}] built in {time.perf_counter()-clock:.0f}s: {len(keys)} pairs", flush=True)
        return eng

    def feats(self, i: int) -> dict[str, tuple[float, bool]]:
        return feats_of(self.V, self.P, i)

    # ------------------------------------------------------------------ deciding
    def decide_one(self, i: int, model: LogisticModel, settings: Settings,
                   feats: dict | None = None) -> tuple[Decision, Decision]:
        lo, hi = self.keys[i]
        fa, fb = self.fps[lo], self.fps[hi]
        la, lb = self.ds.listings[lo], self.ds.listings[hi]
        f = feats if feats is not None else self.feats(i)
        pre = _decide_layers(fa, fb, la, lb, f, self.probes[i], model, settings, False)
        pre = apply_context_rule(pre, f, la, lb, settings, self.hazard)
        post = apply_merge_policy(apply_d43_rule(pre, la, lb, f, settings), la, lb, settings)
        return pre, post

    def decide_all(self, model: LogisticModel, settings: Settings,
                   transform: Callable[[dict], dict] | None = None,
                   only: Iterable[int] | None = None,
                   base: list[Decision] | None = None,
                   scores: np.ndarray | None = None) -> tuple[list[Decision], list[Decision]]:
        """Re-decide `only` (all when None); the rest are copied from `base` with the new score."""
        n = len(self.keys)
        post: list[Decision] = [None] * n  # type: ignore[list-item]
        pre: list[Decision] = [None] * n  # type: ignore[list-item]
        todo = range(n) if only is None else sorted(set(only))
        if only is not None:
            assert base is not None
            for i in range(n):
                d = base[i]
                if scores is not None and d.zone != "veto":
                    d = copy.copy(d)
                    d.score = float(scores[i])
                post[i] = d
        for i in todo:
            f = self.feats(i)
            if transform is not None:
                f = transform(f)
            p0, p1 = self.decide_one(i, model, settings, f)
            pre[i], post[i] = p0, p1
        return pre, post

    # ------------------------------------------------------------------ clustering
    def cluster(self, decisions: list[Decision], settings: Settings,
                transform: Callable[[dict], dict] | None = None) -> Run:
        clock = time.perf_counter()
        slots: dict[tuple[int, int], dict] = {}
        vetoed: set[tuple[int, int]] = set()
        slot_idx = [FIDX[s] for s in FEATURE_SLOTS]
        for i, d in enumerate(decisions):
            if d.veto == UNIT_DESIGNATOR_VETO:
                vetoed.add((d.lo, d.hi))
            if storable({"zone": d.zone, "score": d.score, "evidence": d.evidence},
                         settings.store_floor):
                if transform is None:
                    slots[(d.lo, d.hi)] = {
                        s: ((float(self.V[i, j]), True) if self.P[i, j] else ABSENT)
                        for s, j in zip(FEATURE_SLOTS, slot_idx)}
                else:
                    f = transform(self.feats(i))
                    slots[(d.lo, d.hi)] = {s: f[s] for s in FEATURE_SLOTS if s in f}
        ordered = sorted(decisions, key=lambda d: (d.lo, d.hi))
        kc = ({(d.lo, d.hi): d.certificate for d in ordered if d.certificate == "K-C"}
              if settings.d43_cluster_price_kc_house_number else None)
        PHASE[0] = "cluster"
        try:
            result = cluster_pairs(ordered, self.ds.listings, self.fps, settings,
                                   frozenset(self.mnl),
                                   relation_for(settings, self.ds.listings, slots, kc),
                                   must_link=frozenset(), machine_vetoes=frozenset(vetoed))
        finally:
            PHASE[0] = "decide"
        clusters = {int(k): sorted(int(m) for m in v) for k, v in result.clusters.items()}
        member_of = {m: k for k, v in clusters.items() for m in v}
        return Run(decisions, clusters, member_of, list(result.conflicts), dict(result.stats),
                   time.perf_counter() - clock)

    def baseline(self) -> Run:
        clock = time.perf_counter()
        pre, post = self.decide_all(self.model, self.settings)
        self.pre = pre
        run = self.cluster(post, self.settings)
        run.seconds = time.perf_counter() - clock
        self.base = run
        return run


# --- vectorised scoring ------------------------------------------------------------------

def terms(model: LogisticModel, V: np.ndarray, P: np.ndarray) -> dict[str, np.ndarray]:
    """Per-feature log-odds columns: value term, presence term; plus each interaction."""
    out: dict[str, np.ndarray] = {}
    Z: dict[str, np.ndarray] = {}
    for name in model.feature_order:
        j = FIDX[name]
        raw = np.where(P[:, j], V[:, j], 0.0)
        scale = model.scales.get(name, 1.0)
        z = (raw - model.means.get(name, 0.0)) / (scale if scale else 1.0)
        Z[name] = z
        out[f"v:{name}"] = model.weights.get(name, 0.0) * z
        out[f"p:{name}"] = model.presence_weights.get(name, 0.0) * P[:, j].astype(np.float64)
    for left, right, coef in model.interactions:
        out[f"i:{left}*{right}"] = coef * Z.get(left, 0.0) * Z.get(right, 0.0)
    return out


def calibrate(model: LogisticModel, p: np.ndarray) -> np.ndarray:
    knots = model.calibration
    if not knots:
        return p
    xs = np.array([k[0] for k in knots])
    ys = np.array([k[1] for k in knots])
    value = np.interp(p, xs, ys)
    return value * (1.0 - CALIBRATION_TIE_BREAK) + CALIBRATION_TIE_BREAK * p


def score_from_terms(model: LogisticModel, cols: dict[str, np.ndarray],
                     drop: set[str] = frozenset(), compensate: bool = True,
                     n: int | None = None) -> np.ndarray:
    total = np.full(n if n is not None else len(next(iter(cols.values()))), model.intercept)
    for key, col in cols.items():
        if key in drop:
            if compensate:
                total = total + float(col.mean())
            continue
        total = total + col
    p = 1.0 / (1.0 + np.exp(-total))
    return calibrate(model, p)


def term_keys_for(model: LogisticModel, features: Iterable[str]) -> set[str]:
    names = set(features)
    keys = {f"v:{n}" for n in names} | {f"p:{n}" for n in names}
    for left, right, _ in model.interactions:
        if left in names or right in names:
            keys.add(f"i:{left}*{right}")
    return keys


def ablated_model(model: LogisticModel, cols: dict[str, np.ndarray],
                  features: Iterable[str], compensate: bool = True) -> LogisticModel:
    """The same model with the named features' log-odds terms replaced by their cohort mean."""
    names = set(features)
    drop = term_keys_for(model, names)
    shift = sum(float(cols[k].mean()) for k in drop if k in cols) if compensate else 0.0
    out = copy.deepcopy(model)
    for name in names:
        if name in out.weights:
            out.weights[name] = 0.0
        if name in out.presence_weights:
            out.presence_weights[name] = 0.0
    out.interactions = [(a, b, 0.0 if (a in names or b in names) else c)
                        for a, b, c in out.interactions]
    out.intercept = model.intercept + shift
    return out


def score_class(eng: Engine, scores: np.ndarray) -> np.ndarray:
    """0 reject, 1 band, 2 model-merge band, +10 when at/above E63's floor."""
    s = eng.settings
    cuts = np.array([
        (stratum_t_hi({"same_source": ((float(eng.V[i, FIDX['same_source']]), True)
                                       if eng.P[i, FIDX['same_source']] else ABSENT)},
                      None, s) or 2.0)
        for i in range(len(eng.keys))])
    cls = np.where(scores >= cuts, 2, np.where(scores > s.t_lo, 1, 0))
    return cls + 10 * (scores >= s.context_rule_min_score)


# --- measuring ----------------------------------------------------------------------------

def copairs(run: Run) -> set[tuple[int, int]]:
    out = set()
    for members in run.clusters.values():
        for x, a in enumerate(members):
            for b in members[x + 1:]:
                out.add((a, b))
    return out


def group_sets(run: Run) -> set[frozenset]:
    return {frozenset(v) for v in run.clusters.values() if len(v) > 1}


def load_operator(eng: Engine) -> dict[str, Any]:
    ids = set(eng.ds.listings)
    ops = yard_mod.load_operator_pairs([LABELS / "operator_merges.jsonl"])
    merge_pairs = {k for k in ops.pairs if k[0] in ids and k[1] in ids}
    latest: dict[tuple[int, int], tuple[str, str, str]] = {}
    for line in (LABELS / "operator_labels.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        k = pair_key(row["listing_lo"], row["listing_hi"])
        if k[0] not in ids or k[1] not in ids:
            continue
        stamp = str(row.get("decided_at") or "")
        prev = latest.get(k)
        if prev is None or stamp >= prev[0]:
            latest[k] = (stamp, str(row.get("verdict")), str(row.get("source")))
    same = {k for k, v in latest.items() if v[1] == "same"}
    diff = {k for k, v in latest.items() if v[1] != "same"}
    explicit_same = {k for k, v in latest.items() if v[1] == "same" and v[2] == "explicit"}
    explicit_diff = {k for k, v in latest.items() if v[1] != "same" and v[2] == "explicit"}
    return {"merge_pairs": merge_pairs, "same": same, "diff": diff,
            "explicit_same": explicit_same, "explicit_diff": explicit_diff}


def together(run: Run, pairs: Iterable[tuple[int, int]]) -> int:
    m = run.member_of
    return sum(1 for a, b in pairs if a in m and m.get(a) == m.get(b))


def compare(eng: Engine, arm: Run, ops: dict[str, Any] | None = None) -> dict[str, Any]:
    base = eng.base
    assert base is not None
    zones_b: dict[str, int] = {}
    zones_a: dict[str, int] = {}
    flips = {"merge->band": 0, "merge->reject": 0, "merge->veto": 0, "band->merge": 0,
             "reject->merge": 0, "band->reject": 0, "reject->band": 0, "other": 0}
    merge_b = merge_a = 0
    reasons_gain: dict[str, int] = {}
    reasons_lost: dict[str, int] = {}
    for db, da in zip(base.decisions, arm.decisions):
        zones_b[db.zone] = zones_b.get(db.zone, 0) + 1
        zones_a[da.zone] = zones_a.get(da.zone, 0) + 1
        merge_b += db.zone == "merge"
        merge_a += da.zone == "merge"
        if db.zone != da.zone:
            key = f"{db.zone}->{da.zone}"
            flips[key if key in flips else "other"] += 1
            if da.zone == "merge":
                r = da.reason.split(":")[0] if not da.certificate else f"cert:{da.certificate}"
                reasons_gain[r] = reasons_gain.get(r, 0) + 1
            if db.zone == "merge":
                r = db.reason.split(":")[0] if not db.certificate else f"cert:{db.certificate}"
                reasons_lost[r] = reasons_lost.get(r, 0) + 1
    merge_flips = sum(v for k, v in flips.items() if k.startswith("merge->") or k.endswith("->merge"))
    cb, ca = copairs(base), copairs(arm)
    gb, ga = group_sets(base), group_sets(arm)
    moved = set()
    for g in (gb ^ ga):
        moved |= set(g)
    out = {
        "zones": zones_a,
        "merge": merge_a, "merge_base": merge_b,
        "merge_flips": merge_flips,
        "merge_flip_share": merge_flips / merge_b if merge_b else 0.0,
        "flips": {k: v for k, v in flips.items() if v},
        "merge_gained_by": reasons_gain, "merge_lost_by": reasons_lost,
        "groups": len(ga), "groups_base": len(gb),
        "groups_differing": len(ga - gb), "groups_base_only": len(gb - ga),
        "listings_in_groups": sum(len(g) for g in ga),
        "copairs": len(ca), "copairs_gained": len(ca - cb), "copairs_lost": len(cb - ca),
        "listings_moved": len(moved),
        "conflicts": len(arm.conflicts),
    }
    if ops is not None:
        for label in ("merge_pairs", "same", "diff", "explicit_same", "explicit_diff"):
            pairs = ops[label]
            out[f"op_{label}_n"] = len(pairs)
            out[f"op_{label}_together"] = together(arm, pairs)
            out[f"op_{label}_together_base"] = together(base, pairs)
    return out


def decision_row(d: Decision) -> dict[str, Any]:
    return d.to_json()


def verify_against(eng: Engine, pairs_path: Path) -> dict[str, Any]:
    """The stored generation's rows against this module's FULL decisions (zone, reason, score)."""
    by_key = {(d.lo, d.hi): d for d in eng.base.decisions}
    n = same = diff_zone = diff_reason = diff_score = missing = 0
    examples = []
    with gzip.open(pairs_path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            n += 1
            d = by_key.get((row["lo"], row["hi"]))
            if d is None:
                missing += 1
                continue
            ok = True
            if d.zone != row["zone"]:
                diff_zone += 1
                ok = False
            if d.reason != row["reason"]:
                diff_reason += 1
                ok = False
            if abs(d.score - float(row["score"])) > 1e-12:
                diff_score += 1
                ok = False
            if ok:
                same += 1
            elif len(examples) < 10:
                examples.append({"key": [row["lo"], row["hi"]], "stored": [row["zone"], row["reason"], row["score"]],
                                 "mine": [d.zone, d.reason, d.score]})
    stored_mine = sum(1 for d in eng.base.decisions
                      if storable({"zone": d.zone, "score": d.score, "evidence": d.evidence},
                                  eng.settings.store_floor))
    return {"stored_rows": n, "identical": same, "zone_differs": diff_zone,
            "reason_differs": diff_reason, "score_differs": diff_score, "missing": missing,
            "mine_storable": stored_mine, "examples": examples}
