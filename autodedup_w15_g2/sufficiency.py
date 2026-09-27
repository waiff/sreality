"""Label sufficiency: the learning curve of the refit on a sealed town split, per scope, and the
number of sealed labels needed to SEE a difference from w6_gold at all.

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.sufficiency
"""
from __future__ import annotations

import collections
import json
import math

import numpy as np

from autodedup_w15_g2.facts7 import first_fact
from autodedup_w15_g2.fit import ground
from autodedup_w15_g2.learn import W6, Model, auc, load_cohort
from autodedup_w15_g2.paths import OUT

SIZES = (100, 200, 400, 800, 1600, 3200)
SEEDS = (1, 2, 3, 4, 5)


def scope(c, k) -> str:
    a, b = c.recs[k[0]], c.recs[k[1]]
    main = a.cmain if a.cmain == b.cmain else f"{a.cmain}/{b.cmain}"
    return f"{main}|{a.ctype}"


def hanley_mcneil(a: float, n_pos: int, n_neg: int) -> float:
    q1, q2 = a / (2 - a), 2 * a * a / (1 + a)
    return math.sqrt((a * (1 - a) + (n_pos - 1) * (q1 - a * a) + (n_neg - 1) * (q2 - a * a))
                     / (n_pos * n_neg))


def needed_negatives(a: float, delta: float, ratio: float = 3.0, r: float = 0.6,
                     z: float = 1.645) -> int:
    """Sealed negatives (with `ratio` positives each) so a one-sided paired test sees `delta`."""
    for n_neg in range(10, 100000, 10):
        se = hanley_mcneil(a, int(ratio * n_neg), n_neg)
        se_diff = math.sqrt(2 * se * se * (1 - r))
        if z * se_diff <= delta:
            return n_neg
    return -1


def main() -> None:
    c = load_cohort("trial")
    g = ground(c, "none")
    w6 = W6().score(c.V, c.P)
    nofact = np.array([first_fact(c.recs[a], c.recs[b]) is None for a, b in c.keys])
    towns = sorted({a for a, _ in g.row_blocks})
    scopes = np.array([scope(c, c.keys[i]) for i in g.idx])
    out: dict = {"curve": [], "scopes": {}, "power": []}
    rng = np.random.default_rng(20260927)
    for kind in ("hgb", "lr"):
        for n in SIZES:
            for seed in SEEDS:
                preds = np.full(len(g.idx), np.nan)
                for town in towns:
                    test = np.array([pair == (town, town) for pair in g.blocks])
                    pool = np.where(np.array([town not in pair for pair in g.blocks]))[0]
                    if n < len(pool):
                        pool = np.random.default_rng(seed * 1000 + n).choice(pool, n, replace=False)
                    if len(set(g.y[pool])) < 2:
                        continue
                    m = Model(kind).fit(g.V[g.idx[pool]], g.P[g.idx[pool]], g.y[pool], g.w[pool])
                    preds[test] = m.raw(g.V[g.idx[test]], g.P[g.idx[test]])
                ok = ~np.isnan(preds)
                row = {"kind": kind, "n_train_per_fold": n, "seed": seed}
                for name, sel in (("operator", g.origin == "operator"),
                                  ("judge", np.isin(g.origin, ["judge_trial", "w6"])),
                                  ("all", np.ones(len(g.idx), bool))):
                    for tag, mm in (("", ok & sel), ("_nofact", ok & sel & nofact[g.idx])):
                        row[name + tag] = auc(preds[mm], g.y[mm])
                        row["w6_" + name + tag] = auc(w6[g.idx[mm]], g.y[mm])
                out["curve"].append(row)
                print(json.dumps(row), flush=True)
                if n >= len(g.idx):
                    break
    # per scope: label counts and full-data out-of-town AUC of hgb vs w6_gold (in-sample)
    preds = np.full(len(g.idx), np.nan)
    for town in towns:
        test = np.array([pair == (town, town) for pair in g.blocks])
        train = np.array([town not in pair for pair in g.blocks])
        m = Model("hgb").fit(g.V[g.idx[train]], g.P[g.idx[train]], g.y[train], g.w[train])
        preds[test] = m.raw(g.V[g.idx[test]], g.P[g.idx[test]])
    for s in sorted(set(scopes)):
        sel = scopes == s
        ok = sel & ~np.isnan(preds)
        out["scopes"][s] = {
            "labels": int(sel.sum()), "pos": int(g.y[sel].sum()), "neg": int((g.y[sel] == 0).sum()),
            "operator_pos": int((sel & (g.origin == "operator") & (g.y == 1)).sum()),
            "operator_neg": int((sel & (g.origin == "operator") & (g.y == 0)).sum()),
            "hgb_auc": auc(preds[ok], g.y[ok]), "w6_auc_in_sample": auc(w6[g.idx[ok]], g.y[ok]),
        }
    for a in (0.95, 0.97, 0.99):
        for delta in (0.005, 0.01, 0.02):
            out["power"].append({"auc": a, "delta": delta,
                                 "sealed_negatives_needed": needed_negatives(a, delta),
                                 "sealed_positives_needed": 3 * needed_negatives(a, delta)})
    (OUT / "sufficiency.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out["scopes"], indent=1))
    print(json.dumps(out["power"], indent=1))


if __name__ == "__main__":
    main()
