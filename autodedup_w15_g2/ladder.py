"""The ladder (w31 + w6_gold) as the census replica runs it, vendored from the W15 C2 census
(`autodedup_w15_census/c2lib.py` on branch w15/census-c2): features computed ONCE per cohort and
cached; every arm re-decides with the real `decide_pair` layers and re-clusters with the real
`cluster_pairs`. No must-link and no must-not-link is loaded: the operator rulings are TEST labels.

`DISABLED` knocks fact readers out of every `distinguishing_facts` call (gate, promote, cluster),
which is how the fold-to-seven-facts arm is measured on the ladder itself."""
from __future__ import annotations

import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from autodedup import d43 as d43_mod
from autodedup import decide as decide_mod
from autodedup import indistinguishable as ind_mod
from autodedup.blocking import generate_pairs
from autodedup.cluster import cluster_pairs
from autodedup.d43 import relation_for
from autodedup.dataset import load
from autodedup.decide import (Decision, _decide_layers, apply_context_rule, apply_d43_rule,
                              apply_merge_policy)
from autodedup.features import ABSENT, FEATURE_ORDER, FeatureContext, pair_features
from autodedup.fingerprint import build_all
from autodedup.guards import UNIT_DESIGNATOR_VETO
from autodedup.harness import load_model
from autodedup.hazard_context import ContextIndex
from autodedup.indistinguishable import FEATURE_SLOTS
from autodedup.model import LogisticModel
from autodedup.settings import Settings
from autodedup.store_score import storable

from autodedup_w15_g2.paths import C2_CACHE, CACHE, COHORTS

REPO = Path(__file__).resolve().parents[1]
ADV_CACHE = Path("/home/hejtm/autodedup-artifacts/w15/census/adversary/runs")
SETTINGS = REPO / "autodedup/settings/w31.json"
MODEL = REPO / "autodedup/models/w6_gold.json"
F = len(FEATURE_ORDER)
FIDX = {name: i for i, name in enumerate(FEATURE_ORDER)}

_ORIGINAL_DF = ind_mod.distinguishing_facts
DISABLED: set[str] = set()


def _hooked(a, b, feats=None, settings=None, mode=ind_mod.PROMOTE):
    facts = _ORIGINAL_DF(a, b, feats, settings, mode)
    if DISABLED:
        facts = [f for f in facts if f.name not in DISABLED]
    return facts


for _module in (ind_mod, decide_mod, d43_mod):
    _module.distinguishing_facts = _hooked


def feats_row(V: np.ndarray, P: np.ndarray, i: int) -> dict[str, tuple[float, bool]]:
    return {name: ((float(V[i, j]), True) if P[i, j] else ABSENT)
            for j, name in enumerate(FEATURE_ORDER)}


@dataclass
class Run:
    decisions: list[Decision]
    clusters: dict[int, list[int]]
    conflicts: list[Any]
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
    ctx: Any = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def build(cls, name: str) -> "Engine":
        clock = time.perf_counter()
        ds = load(str(COHORTS[name]))
        settings = Settings.from_json(SETTINGS)
        model = load_model(str(MODEL))
        fps = build_all(ds, settings)
        hazard = ContextIndex.build(ds.listings, ds.images_by_listing)
        cache = next((p for p in (C2_CACHE / f"feats_{name}.pkl", CACHE / f"feats_{name}.pkl")
                      if p.is_file()), None)
        ctx = None
        if cache is not None:
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
            # the adversary's C1-replica cache (same code base afd121ae): {(lo, hi): features}
            adv_path = ADV_CACHE / f"cache_cohort{name[1:]}.pkl"
            adv: dict = {}
            if name.startswith("c") and adv_path.is_file():
                with open(adv_path, "rb") as handle:
                    adv = pickle.load(handle)
                print(f"[{name}] adversary feature cache {adv_path} ({len(adv)} pairs)", flush=True)
            for i, (lo, hi) in enumerate(keys):
                feats = adv.get((lo, hi))
                if feats is None:
                    feats = pair_features(fps[lo], fps[hi], ds.listings[lo], ds.listings[hi],
                                          ds.images(lo), ds.images(hi), ctx, settings)
                for j, fname in enumerate(FEATURE_ORDER):
                    value, present = feats.get(fname, ABSENT)
                    V[i, j] = float(value)
                    P[i, j] = bool(present)
                if i % 20000 == 0:
                    print(f"[{name}] features {i}/{len(keys)} {time.perf_counter()-clock:.0f}s",
                          flush=True)
            CACHE.mkdir(parents=True, exist_ok=True)
            with open(CACHE / f"feats_{name}.pkl", "wb") as handle:
                pickle.dump((keys, probes, V, P), handle, protocol=5)
        eng = cls(name, ds, settings, model, fps, keys, probes, V, P, hazard, ctx)
        print(f"[{name}] built in {time.perf_counter()-clock:.0f}s: {len(keys)} pairs", flush=True)
        return eng

    def context(self) -> Any:
        if self.ctx is None:
            self.ctx = FeatureContext.build(self.fps, self.settings, self.ds)
            self.ctx.index_attrs(self.fps, self.ds.listings)
        return self.ctx

    def features_for(self, pairs: list[tuple[int, int]]) -> tuple[np.ndarray, np.ndarray]:
        """Feature rows for pairs retrieval never produced (labelled pairs outside the candidates)."""
        ctx = self.context()
        V = np.zeros((len(pairs), F), dtype=np.float64)
        P = np.zeros((len(pairs), F), dtype=bool)
        for i, (lo, hi) in enumerate(pairs):
            feats = pair_features(self.fps[lo], self.fps[hi], self.ds.listings[lo],
                                  self.ds.listings[hi], self.ds.images(lo), self.ds.images(hi),
                                  ctx, self.settings)
            for j, fname in enumerate(FEATURE_ORDER):
                value, present = feats.get(fname, ABSENT)
                V[i, j] = float(value)
                P[i, j] = bool(present)
        return V, P

    def decide_all(self) -> list[Decision]:
        out: list[Decision] = []
        for i, (lo, hi) in enumerate(self.keys):
            fa, fb = self.fps[lo], self.fps[hi]
            la, lb = self.ds.listings[lo], self.ds.listings[hi]
            f = feats_row(self.V, self.P, i)
            pre = _decide_layers(fa, fb, la, lb, f, self.probes[i], self.model, self.settings,
                                 False)
            pre = apply_context_rule(pre, f, la, lb, self.settings, self.hazard)
            out.append(apply_merge_policy(apply_d43_rule(pre, la, lb, f, self.settings), la, lb,
                                          self.settings))
        return out

    def cluster(self, decisions: list[Decision]) -> Run:
        clock = time.perf_counter()
        slots: dict[tuple[int, int], dict] = {}
        vetoed: set[tuple[int, int]] = set()
        slot_idx = [FIDX[s] for s in FEATURE_SLOTS]
        for i, d in enumerate(decisions):
            if d.veto == UNIT_DESIGNATOR_VETO:
                vetoed.add((d.lo, d.hi))
            if storable({"zone": d.zone, "score": d.score, "evidence": d.evidence},
                         self.settings.store_floor):
                slots[(d.lo, d.hi)] = {
                    s: ((float(self.V[i, j]), True) if self.P[i, j] else ABSENT)
                    for s, j in zip(FEATURE_SLOTS, slot_idx)}
        ordered = sorted(decisions, key=lambda d: (d.lo, d.hi))
        kc = ({(d.lo, d.hi): d.certificate for d in ordered if d.certificate == "K-C"}
              if self.settings.d43_cluster_price_kc_house_number else None)
        result = cluster_pairs(ordered, self.ds.listings, self.fps, self.settings, frozenset(),
                               relation_for(self.settings, self.ds.listings, slots, kc),
                               must_link=frozenset(), machine_vetoes=frozenset(vetoed))
        clusters = {int(k): sorted(int(m) for m in v) for k, v in result.clusters.items()}
        return Run(decisions, clusters, list(result.conflicts), dict(result.stats),
                   time.perf_counter() - clock)

    def run(self, disabled: set[str] = frozenset()) -> Run:
        clock = time.perf_counter()
        DISABLED.clear()
        DISABLED.update(disabled)
        try:
            run = self.cluster(self.decide_all())
        finally:
            DISABLED.clear()
        run.seconds = time.perf_counter() - clock
        return run
