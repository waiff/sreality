"""The operating curve: every saved sealed score run through the model-first engine over a grid of
merge cuts, beside the ladder's FULL and FOLD7 points, so engines are compared at MATCHED operating
points instead of at one nominal cut (a cut means nothing across two calibrations).

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.sweep <out tag> <cohort> [<cohort> ...]

Scores (cache/p_<run>_<cohort>_<key>.npy): s6 = boosting / logistic / MLP on the 60 features, one
isotonic map fitted on the trial's out-of-town rows; s8 = s6's boosting refitted with c5 in the
ground; s10 = boosting on the 60 features + the 64 readers as 0/1 columns (`r64`)."""
from __future__ import annotations

import json
import sys
import time

import numpy as np

from autodedup.body_align import aligned_difference
from autodedup.dataset import load

from autodedup_w15_g2.engine_g2 import EngineCfg, run_engine
from autodedup_w15_g2.facts7 import FactCfg, first_fact
from autodedup_w15_g2.learn import load_cohort
from autodedup_w15_g2.metrics import measure
from autodedup_w15_g2.paths import COHORTS, OUT
from autodedup_w15_g2.sealed import adversary

FACTS = FactCfg(colive_floor_delta=1, colive_price_tol=0.005, colive_area_tol=0.005)
GRID = (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.98)
SHORT = (0.8, 0.9, 0.95)
SOURCES = [  # (label, run, key, cuts)
    ("hgb", "s6", "hgb", GRID),
    ("hgb+c5", "s8", "hgb", GRID),
    ("hgb+r64", "s10", "hgb", GRID),
    ("w6_gold", "s6", "w6_gold", (0.5, 0.9545, 0.9788, 0.99)),
    ("lr", "s6", "lr", SHORT),
    ("mlp", "s6", "mlp", SHORT),
    ("hgb[op]", "s6", "hgb-op", SHORT),
    ("hgb[op+c7]", "s6", "hgb-op_c7", SHORT),
    ("hgb[judge]", "s6", "hgb-judge", SHORT),
]
KEEP = ("groups", "adverts_grouped", "copairs", "max_group", "op_same_together", "op_same",
        "op_diff_apart", "op_diff", "op_diff_coclustered", "c7_one_pairs_together", "c7_one_pairs",
        "c7_fused_pairs_apart", "c7_fused_pairs", "judge_pos_together", "judge_pos", "judge_neg_apart",
        "judge_neg", "screen_flagged_groups", "groups_not_in_ladder", "screen_flagged_not_in_ladder",
        "ladder_groups_not_here", "copairs_gained_vs_ladder", "copairs_lost_vs_ladder")


def slim(m: dict) -> dict:
    return {k: m[k] for k in KEEP if k in m}


def main(tag: str, names: list[str]) -> None:
    out = {}
    for n in names:
        clock = time.perf_counter()
        c = load_cohort(n)
        desc = {lid: l.description for lid, l in load(str(COHORTS[n])).listings.items()}

        def unit_text(a: int, b: int) -> str | None:
            return "unit_text" if aligned_difference(desc[a], desc[b], 0.6, True) is not None else None

        ref = c.ladder["FULL"]["clusters"]
        res = {"ladder": {}, "engine": {}}
        for arm in ("FULL", "FOLD7"):
            if arm in c.ladder:
                m = slim(measure(n, c.ladder[arm]["clusters"], c.recs, c.labels, ref))
                m["band"] = int((c.ladder[arm]["zone"] == "band").sum())
                m["adversary"] = adversary(n, c.ladder[arm]["clusters"], c.recs)
                res["ladder"][arm] = m
        pf = [first_fact(c.recs[a], c.recs[b], FACTS) for a, b in c.cand_keys]
        lazy: dict = {}
        print(f"[{n}] loaded + facts {time.perf_counter()-clock:.0f}s", flush=True)
        for label, run_tag, key, cuts in SOURCES:
            path = OUT / f"cache/p_{run_tag}_{n}_{key}.npy"
            if not path.is_file():
                continue
            p = np.load(path)[: c.n_cand]
            for t in cuts:
                t0 = time.perf_counter()
                run = run_engine(c.cand_keys, p, c.recs,
                                 EngineCfg(t_merge=t, t_band=0.2, facts=FACTS, lazy_fact=unit_text),
                                 pair_facts=pf, lazy_memo=lazy)
                m = slim(measure(n, run.groups, c.recs, c.labels, ref))
                m.update({"band": run.band, "merge_edges": run.edges, "refused_group": run.refused_fact,
                          "adversary": adversary(n, run.groups, c.recs),
                          "seconds": round(time.perf_counter() - t0, 1)})
                res["engine"][f"{label}@{t}"] = m
                print(n, f"{label}@{t}", {k: m[k] for k in ("groups", "copairs", "op_same_together",
                                                              "op_diff_apart", "screen_flagged_groups",
                                                              "screen_flagged_not_in_ladder", "band",
                                                              "judge_neg_apart", "c7_fused_pairs_apart")},
                      m["seconds"], flush=True)
        out[n] = res
        (OUT / f"sweep_{tag}_{n}.json").write_text(json.dumps(res, indent=1, default=str))
        print(f"[{n}] done {time.perf_counter()-clock:.0f}s", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
