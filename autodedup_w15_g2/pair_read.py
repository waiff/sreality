"""Pair cards for the labelled pairs one engine co-clusters and the other does not (a D83 read of
the judge-negative / C7-fused side): both adverts' columns, text head, price path, live window.

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.pair_read <tag> <cohort> <n> [origin]
"""
from __future__ import annotations

import json
import random
import sys

import numpy as np

from autodedup.dataset import load

from autodedup_w15_g2.engine_g2 import EngineCfg, run_engine
from autodedup_w15_g2.facts7 import FactCfg, colive_days, facts, first_fact
from autodedup_w15_g2.learn import load_cohort
from autodedup_w15_g2.metrics import copairs
from autodedup_w15_g2.paths import COHORTS, OUT

F7S = FactCfg(colive_floor_delta=1, colive_price_tol=0.005)


def main(tag: str, name: str, n: int, origin: str = "judge_trial,w6") -> None:
    c = load_cohort(name)
    p = np.load(OUT / f"cache/p_{tag}_{name}_hgb.npy")[: c.n_cand]
    pf = [first_fact(c.recs[a], c.recs[b], F7S) for a, b in c.cand_keys]
    run = run_engine(c.cand_keys, p, c.recs, EngineCfg(t_merge=0.9, t_band=0.2, facts=F7S), pair_facts=pf)
    mine, lad = copairs(run.groups), copairs(c.ladder["FULL"]["clusters"])
    origins = set(origin.split(","))
    neg = sorted(k for k, lab in c.labels.items() if lab.y == 0 and lab.origin in origins)
    only_mf = [k for k in neg if k in mine and k not in lad]
    only_lad = [k for k in neg if k in lad and k not in mine]
    print(f"{name}: negatives {len(neg)}; together only in MF {len(only_mf)}, only in ladder {len(only_lad)}")
    pick = random.Random(20260927).sample(only_mf, min(n, len(only_mf)))
    ds = load(str(COHORTS[name]))
    pos = {k: i for i, k in enumerate(c.keys)}
    for a, b in pick:
        lab = c.labels[(a, b)]
        i = pos.get((a, b))
        print(f"## {a} x {b}  label {lab.src} y={lab.y}  p={p[i] if i is not None and i < len(p) else None}"
              f"  facts7s={facts(c.recs[a], c.recs[b], F7S)}  colive={colive_days(c.recs[a], c.recs[b])}")
        for m in (a, b):
            l = ds.listings[m]
            path_ = [int(x) for _, x in (l.price_history or []) if x][-3:]
            print(f"  {m} {l.source} {l.category_type}/{l.category_main} {l.disposition} {l.area_m2} "
                  f"fl{l.floor}/{l.total_floors} {l.price} {path_} {l.location.street_key} "
                  f"{l.location.house_number or ''} {(l.first_seen_at or '')[:10]}..{(l.last_seen_at or '')[:10]} "
                  f"brk {(l.broker_key or '')[:5]} | {' '.join((l.description or '').split())[:260]}")
    (OUT / f"pair_read_{tag}_{name}.json").write_text(json.dumps({"only_mf": only_mf, "only_ladder": only_lad,
                                                                   "sample": pick}))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]), *(sys.argv[4:5]))
