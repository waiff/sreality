"""What the ladder's positive rules add on top of MF-H: its proofs (K-B / K-C / K-R certificates)
and its demonstration (D43 promotion + D50) fed to MF-H as p = 1 edges. The seven facts, the
must-not-links and the group check still apply to every such edge.

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.arms4 <tag> <cohort> [...]"""
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

PROOFS = {"K-B", "K-C", "K-R"}


def main(tag: str, names: list[str]) -> None:
    for n in names:
        c = load_cohort(n)
        desc = {lid: l.description for lid, l in load(str(COHORTS[n])).listings.items()}

        def unit_text(a: int, b: int) -> str | None:
            return "unit_text" if aligned_difference(desc[a], desc[b], 0.6, True) is not None else None

        p0 = np.load(OUT / f"cache/p_{'s8' if n == 'c5' else 's6'}_{n}_hgb.npy")[: c.n_cand]
        lad = c.ladder["FULL"]
        merge = lad["zone"] == "merge"
        cert = np.array([x or "" for x in lad["certificate"]])
        promo = np.array([(r or "").startswith("d43_promote") for r in lad["reason"]])
        proof = merge & np.isin(cert, list(PROOFS))
        pf = [first_fact(c.recs[a], c.recs[b], FACTS) for a, b in c.cand_keys]
        lazy: dict = {}
        res = {}
        for arm, force in (("MF-H", np.zeros(c.n_cand, bool)), ("MF-H+proofs", proof),
                           ("MF-H+proofs+demo", proof | (merge & promo))):
            p = np.where(force, 1.0, p0)
            run = run_engine(c.cand_keys, p, c.recs,
                             EngineCfg(t_merge=0.8, t_band=0.2, t_neg=0.2, facts=FACTS, lazy_fact=unit_text),
                             pair_facts=pf, lazy_memo=lazy)
            m = slim(measure(n, run.groups, c.recs, c.labels, c.ladder["FULL"]["clusters"]))
            m.update({"band": run.band, "adversary": adversary(n, run.groups, c.recs), "forced": int(force.sum())})
            res[arm] = m
            print(n, arm, {k: m[k] for k in ("groups", "copairs", "op_same_together", "op_diff_apart", "judge_neg_apart",
                                                "c7_fused_pairs_apart", "c7_one_pairs_together", "screen_flagged_groups",
                                                "screen_flagged_not_in_ladder", "copairs_gained_vs_ladder",
                                                "copairs_lost_vs_ladder", "forced")},
                  "adv_apart", sum(1 for v in m["adversary"].values() if not v["together"]), "/", len(m["adversary"]),
                  flush=True)
        (OUT / f"arms4_{tag}_{n}.json").write_text(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
