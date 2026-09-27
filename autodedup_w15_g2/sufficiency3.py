"""Label sufficiency on the ONE ground with enough negatives: each trial town held out in turn.

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.sufficiency3

Eval rows: the held-out town's labels that are NOT w6_gold's own training labels (operator rulings,
C7 reads, the 2026-09-26 judge trial). w6_gold saw the town's adverts (4,886 of 5,195 trial adverts sit
in its training export), so the comparison is tilted TOWARD w6_gold; a refit that beats it here beats it.
Training pool: every label that touches no advert of the held-out town (the other two trial towns, all
origins, plus c17, c18, c16, c14, c5). Curves: n labels drawn from the whole pool, from the judge tiers
only, and per scope from that scope's labels only; AUC per scope on the same eval rows."""
from __future__ import annotations

import collections
import json
import time

import numpy as np

from autodedup_w15_g2.fit import ground
from autodedup_w15_g2.learn import W6, Model, auc, load_cohort
from autodedup_w15_g2.paths import OUT

SIZES = (100, 250, 500, 1000, 2000, 4000)
SEEDS = (1, 2, 3)
OTHER = ("c17", "c18", "c16", "c14", "c5")
SCOPES = ("byt|prodej", "byt|pronajem", "dum|prodej", "komercni|*")


def scope(recs, k) -> str:
    a, b = recs[k[0]], recs[k[1]]
    main = a.cmain if a.cmain == b.cmain else "mixed"
    deal = a.ctype if a.ctype == b.ctype else "mixed"
    return "komercni|*" if main == "komercni" else f"{main}|{deal}"


def rows(name: str):
    c = load_cohort(name)
    g = ground(c, "none")
    keys = [c.keys[i] for i in g.idx]
    blocks = [(c.recs[a].block, c.recs[b].block) for a, b in keys]
    sc = [scope(c.recs, k) for k in keys]
    return dict(V=g.V[g.idx], P=g.P[g.idx], y=g.y, w=g.w, origin=g.origin, blocks=blocks, scope=np.array(sc))


def main() -> None:
    clock = time.perf_counter()
    trial = rows("trial")
    other = [rows(n) for n in OTHER]
    w6 = W6()
    p_w6 = w6.score(trial["V"], trial["P"])
    towns = sorted({b for pair in trial["blocks"] for b in pair})
    oof: dict[tuple, np.ndarray] = {}
    evalmask = np.zeros(len(trial["y"]), bool)
    curve = []
    for town in towns:
        touch = np.array([town in pair for pair in trial["blocks"]])
        ev = touch & (trial["origin"] != "w6") & np.array([pair[0] == pair[1] for pair in trial["blocks"]])
        evalmask |= ev
        tr = ~touch
        pool = {k: np.concatenate([trial[k][tr]] + [o[k] for o in other]) for k in ("y", "w", "origin", "scope")}
        pool["V"] = np.vstack([trial["V"][tr]] + [o["V"] for o in other])
        pool["P"] = np.vstack([trial["P"][tr]] + [o["P"] for o in other])
        judge = np.isin(pool["origin"], ["w6", "judge_trial"])
        print(f"{town}: eval {int(ev.sum())} ({int((trial['y'][ev] == 0).sum())} neg), pool {len(pool['y'])} "
              f"({int((pool['y'] == 0).sum())} neg), judge pool {int(judge.sum())}", flush=True)
        variants = [("all", np.ones(len(pool["y"]), bool))] + [("judge", judge)] + \
                   [(f"scope:{s}", pool["scope"] == s) for s in SCOPES]
        for vname, sel in variants:
            idx_all = np.where(sel)[0]
            for n in SIZES + (len(idx_all),):
                if n > len(idx_all):
                    continue
                for seed in (SEEDS if n < len(idx_all) else (1,)):
                    rng = np.random.default_rng(seed)
                    idx = idx_all if n == len(idx_all) else rng.choice(idx_all, n, replace=False)
                    if len(set(pool["y"][idx])) < 2:
                        continue
                    m = Model("hgb").fit(pool["V"][idx], pool["P"][idx], pool["y"][idx], pool["w"][idx])
                    key = (vname, "full" if n == len(idx_all) else n, seed)
                    arr = oof.setdefault(key, np.full(len(trial["y"]), np.nan))
                    arr[ev] = m.raw(trial["V"][ev], trial["P"][ev])
                    curve.append({"town": town, "variant": vname, "n": int(n), "seed": seed,
                                  "neg_in_train": int((pool["y"][idx] == 0).sum())})
        print(f"{town} done {time.perf_counter()-clock:.0f}s", flush=True)
    ev = evalmask
    y = trial["y"]
    res = {"eval": {"n": int(ev.sum()), "neg": int((y[ev] == 0).sum()),
                    "by_scope": {s: [int((ev & (trial['scope'] == s) & (y == 1)).sum()),
                                     int((ev & (trial['scope'] == s) & (y == 0)).sum())] for s in SCOPES}},
           "w6_gold": {}, "refit": {}, "curve_rows": curve}
    res["w6_gold"]["all"] = auc(p_w6[ev], y[ev])
    for s in SCOPES:
        m = ev & (trial["scope"] == s)
        res["w6_gold"][s] = auc(p_w6[m], y[m])
    table = collections.defaultdict(list)
    for (vname, n, seed), arr in oof.items():
        ok = ev & ~np.isnan(arr)
        if ok.sum() < ev.sum():
            continue
        row = {"all": auc(arr[ev], y[ev])}
        for s in SCOPES:
            m = ev & (trial["scope"] == s)
            row[s] = auc(arr[m], y[m])
        table[(vname, n)].append(row)
    for (vname, n), rows_ in sorted(table.items(), key=lambda kv: (kv[0][0], str(kv[0][1]).zfill(6))):
        agg = {k: round(float(np.mean([r[k] for r in rows_ if r[k] is not None])), 4)
               for k in rows_[0] if any(r[k] is not None for r in rows_)}
        lo = {k: round(float(np.min([r[k] for r in rows_ if r[k] is not None])), 4)
              for k in rows_[0] if any(r[k] is not None for r in rows_)}
        res["refit"][f"{vname}@{n}"] = {"mean": agg, "min": lo, "seeds": len(rows_)}
        print(f"{vname}@{n}", agg, "min", lo, flush=True)
    print("w6_gold", res["w6_gold"])
    (OUT / "sufficiency3.json").write_text(json.dumps(res, indent=1, default=str))
    print(f"done {time.perf_counter()-clock:.0f}s")


if __name__ == "__main__":
    main()
