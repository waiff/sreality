"""The two mechanics the D83 read asked for, as arms on the headline (boosting, s6, cut 0.8):
learned negative evidence at group grain (`t_neg`: a union is refused when a SCORED cross pair
sits below it) and one portal's floor convention held across time (`same_portal_floor_delta`).

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.arms2 <tag> <cohort> [...]

The read fusions are fixtures of the shared keep-apart list (`adversary`)."""
from __future__ import annotations

import dataclasses
import json
import sys
import time

import numpy as np

from autodedup.body_align import aligned_difference
from autodedup.dataset import load

from autodedup_w15_g2.engine_g2 import EngineCfg, run_engine
from autodedup_w15_g2.facts7 import first_fact
from autodedup_w15_g2.learn import load_cohort
from autodedup_w15_g2.metrics import measure
from autodedup_w15_g2.paths import COHORTS, OUT
from autodedup_w15_g2.sealed import adversary
from autodedup_w15_g2.sweep import FACTS, slim

SPF = dataclasses.replace(FACTS, same_portal_floor_delta=1)
ARMS = [  # (label, run, key, cut, t_neg, facts)
    ("base", "s6", "hgb", 0.8, None, FACTS),
    ("tneg0.2", "s6", "hgb", 0.8, 0.2, FACTS),
    ("tneg0.35", "s6", "hgb", 0.8, 0.35, FACTS),
    ("tneg0.5", "s6", "hgb", 0.8, 0.5, FACTS),
    ("spf1", "s6", "hgb", 0.8, None, SPF),
    ("spf1+tneg0.35", "s6", "hgb", 0.8, 0.35, SPF),
    ("spf1+tneg0.5", "s6", "hgb", 0.8, 0.5, SPF),
    ("spf1+tneg0.35@0.7", "s6", "hgb", 0.7, 0.35, SPF),
    ("spf1+tneg0.35@0.85", "s6", "hgb", 0.85, 0.35, SPF),
    ("r64:spf1+tneg0.35", "s10", "hgb", 0.8, 0.35, SPF),
]


def main(tag: str, names: list[str]) -> None:
    for n in names:
        clock = time.perf_counter()
        c = load_cohort(n)
        desc = {lid: l.description for lid, l in load(str(COHORTS[n])).listings.items()}

        def unit_text(a: int, b: int) -> str | None:
            return "unit_text" if aligned_difference(desc[a], desc[b], 0.6, True) is not None else None

        ref = c.ladder["FULL"]["clusters"]
        lazy: dict = {}
        memo: dict = {}
        res = {}
        for label, run, key, cut, t_neg, cfg in ARMS:
            path = OUT / f"cache/p_{run}_{n}_{key}.npy"
            if not path.is_file():
                continue
            p = np.load(path)[: c.n_cand]
            if id(cfg) not in memo:
                memo[id(cfg)] = [first_fact(c.recs[a], c.recs[b], cfg) for a, b in c.cand_keys]
            run_ = run_engine(c.cand_keys, p, c.recs,
                              EngineCfg(t_merge=cut, t_band=0.2, t_neg=t_neg, facts=cfg, lazy_fact=unit_text),
                              pair_facts=memo[id(cfg)], lazy_memo=lazy)
            m = slim(measure(n, run_.groups, c.recs, c.labels, ref))
            m.update({"band": run_.band, "refused_group_fact": run_.refused_fact,
                      "refused_group_neg": run_.refused_neg, "adversary": adversary(n, run_.groups)})
            res[label] = m
            (OUT / f"cache/groups_{tag}_{n}_{label}.json").write_text(
                json.dumps({str(k): v for k, v in run_.groups.items()}))
            print(n, label, {k: m[k] for k in ("groups", "copairs", "op_same_together", "op_diff_apart",
                                                 "judge_neg_apart", "c7_fused_pairs_apart", "c7_one_pairs_together",
                                                 "screen_flagged_groups", "screen_flagged_not_in_ladder", "band",
                                                 "copairs_gained_vs_ladder", "copairs_lost_vs_ladder")},
                  "adv_apart", sum(1 for v in m["adversary"].values() if not v["together"]), "/", len(m["adversary"]),
                  flush=True)
        (OUT / f"arms2_{tag}_{n}.json").write_text(json.dumps(res, indent=1, default=str))
        print(f"[{n}] done {time.perf_counter()-clock:.0f}s", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
