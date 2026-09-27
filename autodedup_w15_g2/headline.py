"""The G2 headline engine (MF-H) on every cohort, blind and with the operator's rulings loaded (BR).

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.headline <tag> <cohort> [...]

MF-H = the seven facts (co-live strict, lazy body-align unit fact) + boosting on the 60 features,
sealed by town, one isotonic map on the trial's out-of-town rows (run s6; s8 for c5, whose towns
entered the ground there) + merge cut 0.8, band 0.2 + complete-link learned negatives t_neg 0.2."""
from __future__ import annotations

import json
import sys
import time

import numpy as np

from autodedup.body_align import aligned_difference
from autodedup.dataset import load

from autodedup_w15_g2.engine_g2 import EngineCfg, run_engine
from autodedup_w15_g2.facts7 import first_fact
from autodedup_w15_g2.labels_g2 import operator_eval_pairs
from autodedup_w15_g2.learn import load_cohort
from autodedup_w15_g2.metrics import measure
from autodedup_w15_g2.paths import COHORTS, OUT
from autodedup_w15_g2.sealed import adversary
from autodedup_w15_g2.sweep import FACTS, slim

CUT, BAND, T_NEG = 0.8, 0.2, 0.2


def main(tag: str, names: list[str]) -> None:
    out = {}
    for n in names:
        clock = time.perf_counter()
        c = load_cohort(n)
        desc = {lid: l.description for lid, l in load(str(COHORTS[n])).listings.items()}

        def unit_text(a: int, b: int) -> str | None:
            return "unit_text" if aligned_difference(desc[a], desc[b], 0.6, True) is not None else None

        run_tag = "s8" if n == "c5" else "s6"
        p = np.load(OUT / f"cache/p_{run_tag}_{n}_hgb.npy")[: c.n_cand]
        pf = [first_fact(c.recs[a], c.recs[b], FACTS) for a, b in c.cand_keys]
        facts_on = {f: pf.count(f) for f in set(pf) if f}
        lazy: dict = {}
        ref = c.ladder["FULL"]["clusters"]
        res = {"facts_on_candidates": facts_on, "candidates": c.n_cand}
        pos, neg = operator_eval_pairs(set(c.recs))
        for arm, ml, mnl in (("MF-H", (), ()), ("MF-H+rulings", sorted(pos), sorted(neg))):
            t0 = time.perf_counter()
            run = run_engine(c.cand_keys, p, c.recs,
                             EngineCfg(t_merge=CUT, t_band=BAND, t_neg=T_NEG, facts=FACTS, lazy_fact=unit_text),
                             must_link=ml, must_not=mnl, pair_facts=pf, lazy_memo=lazy)
            m = slim(measure(n, run.groups, c.recs, c.labels, ref))
            m.update({"band": run.band, "merge_edges": run.edges, "refused_group_fact": run.refused_fact,
                      "refused_group_neg": run.refused_neg, "lazy": run.fact_pairs.get("lazy_pairs_fired"),
                      "groups_ge10": sum(1 for v in run.groups.values() if len(v) >= 10),
                      "adversary": adversary(n, run.groups), "seconds": round(time.perf_counter() - t0, 1)})
            res[arm] = m
            (OUT / f"cache/groups_{tag}_{n}_{arm}.json").write_text(
                json.dumps({str(k): v for k, v in run.groups.items()}))
            print(n, arm, {k: m[k] for k in ("groups", "copairs", "max_group", "op_same_together", "op_same",
                                                "op_diff_apart", "op_diff", "judge_neg_apart", "c7_fused_pairs_apart",
                                                "screen_flagged_groups", "screen_flagged_not_in_ladder", "band",
                                                "copairs_gained_vs_ladder", "copairs_lost_vs_ladder", "seconds")},
                  flush=True)
        out[n] = res
        (OUT / f"headline_{tag}_{n}.json").write_text(json.dumps(res, indent=1, default=str))
        print(f"[{n}] done {time.perf_counter()-clock:.0f}s", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
