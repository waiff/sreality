"""How many of the ladder's readers the model-first engine needs back as hard facts: MF-H with a reader
set added as vetoes (a firing reader, CLUSTER reading, forbids the pair and every union across it).
The readers are read where the census replica computed them (every candidate and labelled pair).

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.arms5 <tag> <cohort> [...]"""
from __future__ import annotations

import json
import sys

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

TEXT = {"printed_area", "prose_floor", "storey_word", "obec_prose", "street_prose", "cellar_area"}
K10 = {"outdoor_accessory", "accessory_price", "obec_prose", "parcel", "position_designator", "lot_label",
       "printed_designator", "unit_code", "labelled_unit"}
KEPT = {"category_type", "category_main", "area", "stated_area", "printed_area", "headline_area", "cellar_area",
        "extent", "disposition", "floor", "total_floors", "prose_floor", "storey_word", "plot_area",
        "plot_prose_exact", "rental_colive", "price", "charge", "street", "street_prose", "orientation",
        "unit_designator", "body_align", "interior", "floorplan"} | K10
SETS = {"MF-H": set(), "+text6": TEXT, "+K10": K10, "+text6+K10": TEXT | K10, "+kept34": KEPT, "+all64": None}


def main(tag: str, names: list[str]) -> None:
    for n in names:
        c = load_cohort(n)
        desc = {lid: l.description for lid, l in load(str(COHORTS[n])).listings.items()}
        fired = {k: set(f) for k, f in zip(c.keys, c.fired)}
        all64 = set(c.reader_names)
        p = np.load(OUT / f"cache/p_{'s8' if n == 'c5' else 's6'}_{n}_hgb.npy")[: c.n_cand]
        pf0 = [first_fact(c.recs[a], c.recs[b], FACTS) for a, b in c.cand_keys]
        res = {}
        for arm, readers in SETS.items():
            rs = all64 if readers is None else readers

            def veto(a: int, b: int, rs=rs) -> str | None:
                hit = fired.get((a, b), set()) & rs
                if hit:
                    return "reader:" + sorted(hit)[0]
                return "unit_text" if aligned_difference(desc[a], desc[b], 0.6, True) is not None else None

            pf = [f if f is not None else (("reader:" + sorted(fired[k] & rs)[0]) if fired[k] & rs else None)
                  for f, k in zip(pf0, c.cand_keys)]
            run = run_engine(c.cand_keys, p, c.recs,
                             EngineCfg(t_merge=0.8, t_band=0.2, t_neg=0.2, facts=FACTS, lazy_fact=veto),
                             pair_facts=pf)
            m = slim(measure(n, run.groups, c.recs, c.labels, c.ladder["FULL"]["clusters"]))
            m.update({"band": run.band, "adversary": adversary(n, run.groups, c.recs), "readers": len(rs)})
            res[arm] = m
            print(n, arm, len(rs), {k: m[k] for k in ("groups", "copairs", "op_same_together", "op_diff_apart",
                                                        "judge_neg_apart", "c7_fused_pairs_apart", "c7_one_pairs_together",
                                                        "screen_flagged_groups", "copairs_gained_vs_ladder",
                                                        "copairs_lost_vs_ladder", "band")},
                  "adv_apart", sum(1 for v in m["adversary"].values() if not v["together"]), "/", len(m["adversary"]),
                  flush=True)
        (OUT / f"arms5_{tag}_{n}.json").write_text(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
