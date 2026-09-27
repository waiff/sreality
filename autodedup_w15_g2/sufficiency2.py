"""Label sufficiency on SEALED ground: a refit trained on n labels of some towns, read against
w6_gold on labels of towns neither model saw (c17 / c18 / the earlier cohorts; rows whose label came
from w6_gold's own training runs are dropped, so w6_gold is out of sample too).

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.sufficiency2 <eval cohort> [...]
"""
from __future__ import annotations

import collections
import json
import math
import sys

import numpy as np

from autodedup_w15_g2.facts7 import first_fact
from autodedup_w15_g2.fit import ground
from autodedup_w15_g2.learn import W6, Model, auc, load_cohort
from autodedup_w15_g2.paths import OUT

SIZES = (250, 500, 1000, 2000, 4000)
SEEDS = (1, 2, 3, 4, 5)


def scope(recs, k) -> str:
    a, b = recs[k[0]], recs[k[1]]
    main = a.cmain if a.cmain == b.cmain else "mixed"
    return f"{main}|{a.ctype if a.ctype == b.ctype else 'mixed'}"


def hanley_mcneil(a: float, n_pos: int, n_neg: int) -> float:
    q1, q2 = a / (2 - a), 2 * a * a / (1 + a)
    return math.sqrt((a * (1 - a) + (n_pos - 1) * (q1 - a * a) + (n_neg - 1) * (q2 - a * a))
                     / (n_pos * n_neg))


def needed_negatives(a: float, delta: float, ratio: float = 3.0, r: float = 0.6,
                     z: float = 1.645) -> int:
    for n_neg in range(10, 100000, 10):
        se = hanley_mcneil(a, int(ratio * n_neg), n_neg)
        if z * math.sqrt(2 * se * se * (1 - r)) <= delta:
            return n_neg
    return -1


def boot_diff(p1: np.ndarray, p2: np.ndarray, y: np.ndarray, reps: int = 400) -> dict:
    rng = np.random.default_rng(20260927)
    pos, neg = np.where(y == 1)[0], np.where(y == 0)[0]
    d = []
    for _ in range(reps):
        s = np.concatenate([rng.choice(pos, len(pos)), rng.choice(neg, len(neg))])
        d.append(auc(p1[s], y[s]) - auc(p2[s], y[s]))
    d = np.array(d)
    return {"diff": round(float(auc(p1, y) - auc(p2, y)), 4),
            "ci90": [round(float(np.quantile(d, 0.05)), 4), round(float(np.quantile(d, 0.95)), 4)]}


def main(evals: list[str]) -> None:
    cohorts = {n: load_cohort(n) for n in ["trial"] + evals}
    G = {n: ground(c, "none") for n, c in cohorts.items()}
    w6 = W6()
    out: dict = {"inventory": {}, "curve": [], "full": {}, "power": []}
    # the label inventory per scope (every cohort, every origin)
    for n, c in cohorts.items():
        g = G[n]
        inv = collections.Counter()
        for i, row in enumerate(g.idx):
            inv[(scope(c.recs, c.keys[row]), str(g.origin[i]), int(g.y[i]))] += 1
        out["inventory"][n] = {f"{s}|{o}|{y}": v for (s, o, y), v in sorted(inv.items())}
    # the sealed evaluation set: eval cohorts, labels not from w6_gold's training runs
    ev = []
    for n in evals:
        g, c = G[n], cohorts[n]
        keep = g.origin != "w6"
        nof = np.array([first_fact(c.recs[c.keys[r][0]], c.recs[c.keys[r][1]]) is None
                        for r in g.idx])
        ev.append((n, g, keep, nof))
    y_ev = np.concatenate([g.y[k] for _, g, k, _ in ev])
    nof_ev = np.concatenate([nf[k] for _, g, k, nf in ev])
    V_ev = np.vstack([g.V[g.idx[k]] for _, g, k, _ in ev])
    P_ev = np.vstack([g.P[g.idx[k]] for _, g, k, _ in ev])
    w6_ev = w6.score(V_ev, P_ev)
    out["eval_set"] = {"n": int(len(y_ev)), "pos": int(y_ev.sum()), "neg": int((y_ev == 0).sum()),
                       "nofact_neg": int(((y_ev == 0) & nof_ev).sum()),
                       "w6_auc": round(auc(w6_ev, y_ev), 4),
                       "w6_auc_nofact": round(auc(w6_ev[nof_ev], y_ev[nof_ev]), 4)}
    print("eval", out["eval_set"], flush=True)
    tr = G["trial"]
    pools = {"trial_all": np.ones(len(tr.idx), bool), "trial_operator": tr.origin == "operator",
             "trial_judge": np.isin(tr.origin, ["judge_trial", "w6"])}
    for kind in ("hgb", "lr"):
        for pool, sel in pools.items():
            rows = np.where(sel)[0]
            for n in SIZES + (len(rows),):
                if n > len(rows):
                    continue
                for seed in SEEDS:
                    pick = np.random.default_rng(seed * 7919 + n).choice(rows, n, replace=False)
                    if len(set(tr.y[pick])) < 2:
                        continue
                    m = Model(kind).fit(tr.V[tr.idx[pick]], tr.P[tr.idx[pick]], tr.y[pick], tr.w[pick])
                    p = m.raw(V_ev, P_ev)
                    row = {"kind": kind, "pool": pool, "n": n, "seed": seed,
                           "neg_in_train": int((tr.y[pick] == 0).sum()),
                           "auc": round(auc(p, y_ev), 4), "auc_nofact": round(auc(p[nof_ev], y_ev[nof_ev]), 4)}
                    out["curve"].append(row)
                    print(json.dumps(row), flush=True)
                    if n == len(rows):
                        out["full"][f"{kind}:{pool}"] = {**boot_diff(p, w6_ev, y_ev),
                                                         "nofact": boot_diff(p[nof_ev], w6_ev[nof_ev], y_ev[nof_ev])}
                        break
    for a in (0.95, 0.97, 0.99):
        for delta in (0.005, 0.01, 0.02):
            k = needed_negatives(a, delta)
            out["power"].append({"auc": a, "delta": delta, "sealed_negatives": k, "sealed_positives": 3 * k})
    (OUT / f"sufficiency2_{'_'.join(evals)}.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out["full"], indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
