"""Fast engine arms over a saved score: facts and cut variants re-run the model-first engine in
seconds per cohort (no refit; the score is the sealed hgb p a `sealed` run saved).

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.engine_arms <tag> <cohort> [<cohort> ...]
"""
from __future__ import annotations

import json
import sys
import time

import numpy as np

from autodedup.body_align import aligned_difference
from autodedup.dataset import load

from autodedup_w15_g2.engine_g2 import EngineCfg, run_engine
from autodedup_w15_g2.facts7 import FactCfg, first_fact
from autodedup_w15_g2.labels_g2 import operator_eval_pairs
from autodedup_w15_g2.learn import load_cohort
from autodedup_w15_g2.metrics import measure
from autodedup_w15_g2.paths import COHORTS, OUT
from autodedup_w15_g2.sealed import adversary

F7S = dict(colive_floor_delta=1, colive_price_tol=0.005)
FACTS = {
    "f7": FactCfg(),
    "f7s": FactCfg(**F7S),
    "f7sa": FactCfg(**F7S, colive_area_tol=0.005),
    "f7s+ba": FactCfg(**F7S),
    "f7sa+ba": FactCfg(**F7S, colive_area_tol=0.005),
    # the BR shape: the operator's rulings LOADED (must-links contracted, must-not-links refused)
    "f7sa+ba+rulings": FactCfg(**F7S, colive_area_tol=0.005),
}
ARMS = [("hgb", f, 0.9) for f in FACTS] + [("hgb", "f7sa+ba", 0.8), ("hgb", "f7sa+ba", 0.95)]
if "--final" in sys.argv:
    ARMS = [("w6_gold", "f7sa+ba", 0.9788), ("lr", "f7sa+ba", 0.9), ("mlp", "f7sa+ba", 0.9),
            ("hgb", "f7", 0.9), ("hgb", "f7sa+ba", 0.8), ("hgb", "f7sa+ba", 0.9), ("hgb", "f7sa+ba", 0.95),
            ("hgb-op", "f7sa+ba", 0.9), ("hgb-op_c7", "f7sa+ba", 0.9), ("hgb-judge", "f7sa+ba", 0.9),
            ("hgb", "f7sa+ba+rulings", 0.9)]
if "--headline" in sys.argv:
    ARMS = [("w6_gold", "f7sa+ba", 0.9788), ("hgb", "f7", 0.9), ("hgb", "f7sa+ba", 0.9)]
if "--all" in sys.argv:
    FACTS.update({"f7s_floor": FactCfg(colive_floor_delta=1), "f7s_price": FactCfg(colive_price_tol=0.005),
                  "f7s_nostreet": FactCfg(**F7S, street=False), "f7_floor1": FactCfg(floor_delta=1)})
    ARMS += [("hgb", f, 0.9) for f in ("f7s_floor", "f7s_price", "f7s_nostreet", "f7_floor1")]


def main(tag: str, names: list[str]) -> None:
    out = {}
    names = [n for n in names if not n.startswith("--")]
    for n in names:
        c = load_cohort(n)
        ps = {}
        desc = {lid: l.description for lid, l in load(str(COHORTS[n])).listings.items()}

        def unit_text(a: int, b: int) -> str | None:
            return "unit_text" if aligned_difference(desc[a], desc[b], 0.6, True) is not None else None

        ref = c.ladder["FULL"]["clusters"]
        res = {}
        memo = {}
        for key, fname, t in ARMS:
            clock = time.perf_counter()
            if key not in ps:
                ps[key] = np.load(OUT / f"cache/p_{tag}_{n}_{key}.npy")[: c.n_cand]
            p = ps[key]
            if fname not in memo:
                memo[fname] = [first_fact(c.recs[a], c.recs[b], FACTS[fname]) for a, b in c.cand_keys]
            ruled = fname.endswith("+rulings")
            pos, neg = operator_eval_pairs(set(c.recs)) if ruled else (set(), set())
            run = run_engine(c.cand_keys, p, c.recs,
                             EngineCfg(t_merge=t, t_band=0.2, facts=FACTS[fname],
                                       lazy_fact=unit_text if "+ba" in fname else None),
                             must_link=sorted(pos), must_not=sorted(neg), pair_facts=memo[fname])
            m = measure(n, run.groups, c.recs, c.labels, ref)
            m.update({"band": run.band, "merge_edges": run.edges, "refused_fact": run.refused_fact,
                      "adversary": adversary(n, run.groups, c.recs), "fact_pairs": run.fact_pairs,
                      "groups_ge10": sum(1 for v in run.groups.values() if len(v) >= 10),
                      "seconds": round(time.perf_counter() - clock, 1)})
            if key == "hgb" and fname == "f7sa+ba" and t == 0.9:
                with open(OUT / f"cache/groups_{tag}_{n}_final.json", "w") as handle:
                    json.dump({str(k): v for k, v in run.groups.items()}, handle)
            res[f"{key}:{fname}@{t}"] = m
            print(n, f"{key}:{fname}@{t}", {k: m[k] for k in ("groups", "copairs", "op_same_together", "op_same",
                                                        "op_diff_apart", "op_diff", "c7_fused_pairs_apart",
                                                        "c7_fused_pairs", "screen_flagged_groups",
                                                        "screen_flagged_not_in_ladder", "band",
                                                        "judge_neg_apart", "judge_neg")},
                  {k: v["together"] for k, v in m["adversary"].items()}, run.fact_pairs, flush=True)
        out[n] = res
    mode = "final" if "--final" in sys.argv else "headline" if "--headline" in sys.argv else "arms"
    (OUT / f"engine_{mode}_{tag}_{'_'.join(names)}.json").write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
