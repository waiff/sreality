"""Where the model-first engine loses and gains against the ladder, per labelled pair, with the cause.

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.losses <run> <key> <cut> <cohort> [...]

For every operator-same pair the two engines disagree on, and every labelled negative (operator,
C7 fused screen, judge) only one engine co-clusters, the model-first side is explained by the first
cause that applies: `not_candidate` (retrieval never produced the pair; only transitivity can join
it), `fact:<name>` (a stated fact forbids it), `score<cut` (the learned p is under the cut),
`group` (an edge, refused at group grain or joined elsewhere). The ladder side is its zone + reason."""
from __future__ import annotations

import collections
import json
import sys

import numpy as np

from autodedup.body_align import aligned_difference
from autodedup.dataset import load

from autodedup_w15_g2.engine_g2 import EngineCfg, run_engine
from autodedup_w15_g2.labels_g2 import operator_eval_pairs
from autodedup_w15_g2.learn import load_cohort
from autodedup_w15_g2.metrics import copairs
from autodedup_w15_g2.paths import COHORTS, OUT
from autodedup_w15_g2.sweep import FACTS
from autodedup_w15_g2.facts7 import facts, first_fact


def ladder_reason(c, i: int | None) -> str:
    if i is None:
        return "not_candidate"
    zone = str(c.ladder["FULL"]["zone"][i])
    reason = c.ladder["FULL"]["reason"][i] or ""
    cert = c.ladder["FULL"]["certificate"][i] or ""
    head = reason.split(":")[0] if reason else ""
    return f"{zone}|{cert or head}"


def main(run: str, key: str, cut: float, names: list[str]) -> None:
    report = {}
    for n in names:
        c = load_cohort(n)
        desc = {lid: l.description for lid, l in load(str(COHORTS[n])).listings.items()}

        def unit_text(a: int, b: int) -> str | None:
            return "unit_text" if aligned_difference(desc[a], desc[b], 0.6, True) is not None else None

        p_all = np.load(OUT / f"cache/p_{run}_{n}_{key}.npy")
        p = p_all[: c.n_cand]
        pos_of = {k: i for i, k in enumerate(c.keys)}
        pf = [first_fact(c.recs[a], c.recs[b], FACTS) for a, b in c.cand_keys]
        mf = run_engine(c.cand_keys, p, c.recs, EngineCfg(t_merge=cut, t_band=0.2, facts=FACTS,
                                                          lazy_fact=unit_text), pair_facts=pf)
        mf_cp, lad_cp = copairs(mf.groups), copairs(c.ladder["FULL"]["clusters"])

        def mf_cause(k) -> str:
            i = pos_of.get(k)
            f = facts(c.recs[k[0]], c.recs[k[1]], FACTS)
            if f:
                return f"fact:{f[0]}"
            if unit_text(*k):
                return "fact:unit_text"
            if i is None or i >= c.n_cand:
                return "not_candidate"
            if p[i] < cut:
                return f"score<{cut}" + (" (band)" if p[i] >= 0.2 else " (reject)")
            return "group"

        pos, neg = operator_eval_pairs(set(c.recs))
        out = {}
        for side, pairs in (("op_same", pos), ("op_diff", neg)):
            lost = sorted(k for k in pairs if (k in lad_cp) and (k not in mf_cp))
            won = sorted(k for k in pairs if (k in mf_cp) and (k not in lad_cp))
            out[side] = {
                "ladder_only_together": len(lost), "mf_only_together": len(won),
                "ladder_only_cause_in_mf": dict(collections.Counter(mf_cause(k) for k in lost)),
                "ladder_only_ladder_reason": dict(collections.Counter(
                    ladder_reason(c, pos_of.get(k) if pos_of.get(k, 10**12) < c.n_cand else None)
                    for k in lost)),
                "mf_only_ladder_reason": dict(collections.Counter(
                    ladder_reason(c, pos_of.get(k) if pos_of.get(k, 10**12) < c.n_cand else None)
                    for k in won)),
                "examples_ladder_only": [[*k, mf_cause(k), round(float(p_all[pos_of[k]]), 3)
                                          if k in pos_of else None] for k in lost[:25]],
                "examples_mf_only": [[*k, ladder_reason(c, pos_of.get(k) if pos_of.get(k, 10**12) < c.n_cand
                                                        else None)] for k in won[:25]],
            }
        for tag, sel in (("c7_fused", lambda lab: lab.src == "c7_fused"),
                         ("judge_neg", lambda lab: lab.origin in ("judge_trial", "w6") and lab.y == 0)):
            ks = [k for k, lab in c.labels.items() if sel(lab)]
            won = sorted(k for k in ks if k in mf_cp and k not in lad_cp)
            lost = sorted(k for k in ks if k in lad_cp and k not in mf_cp)
            out[tag] = {"mf_only_together": len(won), "ladder_only_together": len(lost),
                        "mf_only_ladder_reason": dict(collections.Counter(
                            ladder_reason(c, pos_of.get(k) if pos_of.get(k, 10**12) < c.n_cand else None)
                            for k in won)),
                        "examples_mf_only": [[*k, round(float(p_all[pos_of[k]]), 3) if k in pos_of else None,
                                              ladder_reason(c, pos_of.get(k) if pos_of.get(k, 10**12) < c.n_cand
                                                            else None)] for k in won[:40]]}
        report[n] = out
        print(n, json.dumps({s: {k: v for k, v in d.items() if not k.startswith("examples")}
                             for s, d in out.items()}), flush=True)
    path = OUT / f"losses_{run}_{key}_{cut}_{'_'.join(names)}.json"
    path.write_text(json.dumps(report, indent=1, default=str))
    print("wrote", path)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4:])
