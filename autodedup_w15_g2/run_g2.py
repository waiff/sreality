"""G2 end to end over the prep pickles: fit on the training ground, score every cohort sealed by
town, run the model-first engine and the ladder arms through one metric set.

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.run_g2 <protocol A|B> <cohort> [<cohort> ...]

Protocol A: train on the trial ground only (every label there: operator, judge tiers, C7 reads);
the trial itself is scored leave-one-town-out; every other cohort by the full trial model.
Protocol B: as A, plus the labels of the OTHER held-out cohorts (never the scored one).
Thresholds are fixed on the trial's out-of-town scores and never read off an evaluation cohort.
"""
from __future__ import annotations

import json
import sys
import time

import numpy as np

from autodedup_w15_g2.engine_g2 import EngineCfg, run_engine
from autodedup_w15_g2.facts7 import FACTS, FactCfg, first_fact
from autodedup_w15_g2.fit import crossfit, ground, sealed_fit
from autodedup_w15_g2.learn import W6, auc, fit_isotonic, load_cohort
from autodedup_w15_g2.metrics import measure
from autodedup_w15_g2.paths import OUT

# (name, model kind, design, t_merge, t_band)
CONFIGS = [
    ("w6_gold", "w6", "none", 0.9788, 0.1823),
    ("lr", "lr", "none", 0.9, 0.2),
    ("mlp", "mlp", "none", 0.9, 0.2),
    ("hgb", "hgb", "none", 0.9, 0.2),
    ("hgb_r64", "hgb", "r64", 0.9, 0.2),
    ("hgb_r64@0.8", "hgb", "r64", 0.8, 0.2),
    ("hgb_r64@0.95", "hgb", "r64", 0.95, 0.2),
]
LADDER_ARMS = ("FULL", "FOLD7", "FOLD7_IMG")
SKIP = ("op_diff_coclustered", "_new_flagged")


def pair_aucs(c, g_idx, g_y, g_origin, score, nofact) -> dict:
    out = {}
    for name, m in (("operator", g_origin == "operator"), ("c7", g_origin == "c7"),
                    ("judge", np.isin(g_origin, ["judge_trial", "w6"])),
                    ("all", np.ones(len(g_idx), bool))):
        for tag, mm in (("", m), ("_nofact", m & nofact[g_idx])):
            a = auc(score[g_idx[mm]], g_y[mm])
            out[name + tag] = {"auc": None if a is None else round(a, 4), "n": int(mm.sum()),
                               "pos": int(g_y[mm].sum())}
    return out


def main(protocol: str, cohorts: list[str]) -> None:
    clock = time.perf_counter()
    trial = load_cohort("trial")
    held = {name: load_cohort(name) for name in cohorts if name != "trial"}
    w6 = W6()
    results: dict = {"protocol": protocol, "configs": CONFIGS, "facts": list(FACTS),
                     "fact_dials": FactCfg().dials(), "cohorts": {}}
    grounds = {}
    for _, kind, mode, _, _ in CONFIGS:
        if kind != "w6" and ("trial", mode) not in grounds:
            grounds[("trial", mode)] = ground(trial, mode)
            for name, c in held.items():
                grounds[(name, mode)] = ground(c, mode)
    # trial out-of-town scores -> calibration + the trial's own engine run
    scores: dict[tuple[str, str], np.ndarray] = {}
    isos = {}
    for cname, kind, mode, t_merge, t_band in CONFIGS:
        key = (kind, mode)
        if kind == "w6" or ("trial", key) in scores:
            continue
        extra = ([grounds[(n, mode)] for n in held] if protocol == "B" else [])
        raw = crossfit(grounds[("trial", mode)], kind, extra)
        g = grounds[("trial", mode)]
        isos[key] = fit_isotonic(raw[g.idx], g.y, g.w)
        scores[("trial", key)] = isos[key].predict(raw)
        print(f"trial {key} crossfit {time.perf_counter()-clock:.0f}s", flush=True)
    for name, c in held.items():
        for cname, kind, mode, t_merge, t_band in CONFIGS:
            key = (kind, mode)
            if kind == "w6" or (name, key) in scores:
                continue
            train = [grounds[("trial", mode)]]
            if protocol == "B":
                train += [grounds[(n, mode)] for n in held if n != name]
            model = sealed_fit(train, kind)
            g = grounds[(name, mode)]
            scores[(name, key)] = isos[key].predict(model.raw(g.V, g.P))
            print(f"{name} {key} sealed fit {time.perf_counter()-clock:.0f}s", flush=True)
    for name, c in [("trial", trial)] + list(held.items()):
        pf = [first_fact(c.recs[a], c.recs[b]) for a, b in c.cand_keys]
        nofact = np.array([f is None for f in pf] + [first_fact(c.recs[a], c.recs[b]) is None
                                                      for a, b in c.keys[c.n_cand:]])
        g0 = ground(c, "none")
        ref = c.ladder["FULL"]["clusters"]
        res = {"adverts": len(c.recs), "candidates": c.n_cand, "labels": len(c.labels),
               "facts_on_candidates": {f: int(sum(1 for x in pf if x == f)) for f in FACTS},
               "ladder": {}, "engine": {}}
        for arm in LADDER_ARMS:
            if arm not in c.ladder:
                continue
            m = measure(name, c.ladder[arm]["clusters"], c.recs, c.labels, ref)
            zone = c.ladder[arm]["zone"]
            m.update({"band": int((zone == "band").sum()), "merge_pairs": int((zone == "merge").sum()),
                      "seconds": round(c.ladder[arm]["seconds"], 1)})
            res["ladder"][arm] = m
        for cname, kind, mode, t_merge, t_band in CONFIGS:
            if kind == "w6":
                p_all = w6.score(c.V, c.P)
            else:
                p_all = scores[(name, (kind, mode))]
            t = time.perf_counter()
            run = run_engine(c.cand_keys, p_all[: c.n_cand], c.recs,
                             EngineCfg(t_merge=t_merge, t_band=t_band), pair_facts=pf)
            m = measure(name, run.groups, c.recs, c.labels, ref)
            m.update({"band": run.band, "merge_edges": run.edges, "refused_fact": run.refused_fact,
                      "seconds": round(time.perf_counter() - t, 2),
                      "pair_auc": pair_aucs(c, g0.idx, g0.y, g0.origin, p_all, nofact)})
            res["engine"][cname] = m
            print(name, cname, {k: v for k, v in m.items() if k not in SKIP and k != "pair_auc"},
                  flush=True)
        results["cohorts"][name] = res
    tag = "_".join(cohorts)
    path = OUT / f"results_{protocol}_{tag}.json"
    path.write_text(json.dumps(results, indent=1, default=str))
    print(f"wrote {path} in {time.perf_counter()-clock:.0f}s")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
