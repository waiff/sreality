"""(a) The seven typed facts, measured: what each one fires on, what it costs (operator 'same' pairs
it forbids), what it buys (operator 'different' pairs it alone keeps apart), and how often the
ladder co-clusters a pair one of the seven forbids.

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.facts_measure <cohort> [<cohort> ...]
"""
from __future__ import annotations

import collections
import json
import sys

from autodedup_w15_g2.facts7 import FACTS, facts
from autodedup_w15_g2.labels_g2 import operator_eval_pairs
from autodedup_w15_g2.learn import load_cohort
from autodedup_w15_g2.metrics import copairs
from autodedup_w15_g2.paths import OUT


def tally(pairs, recs) -> dict:
    any_, sole, first, none = collections.Counter(), collections.Counter(), collections.Counter(), 0
    for a, b in pairs:
        f = facts(recs[a], recs[b])
        if not f:
            none += 1
            continue
        first[f[0]] += 1
        any_.update(f)
        if len(f) == 1:
            sole[f[0]] += 1
    return {"n": len(pairs), "no_fact": none, "first": dict(first), "any": dict(any_),
            "sole": dict(sole)}


def main(names: list[str]) -> None:
    out = {}
    for n in names:
        c = load_cohort(n)
        pos, neg = operator_eval_pairs(set(c.recs))
        labs = collections.defaultdict(list)
        for k, lab in c.labels.items():
            labs[f"{lab.origin}:{lab.y}"].append(k)
        lcp = copairs(c.ladder["FULL"]["clusters"])
        f7cp = copairs(c.ladder["FOLD7"]["clusters"]) if "FOLD7" in c.ladder else set()
        res = {"candidates": tally(c.cand_keys, c.recs),
               "op_same": tally(sorted(pos), c.recs), "op_diff": tally(sorted(neg), c.recs),
               "labels": {k: tally(v, c.recs) for k, v in sorted(labs.items())},
               "ladder_full_copairs": tally(sorted(lcp), c.recs),
               "ladder_fold7_copairs": tally(sorted(f7cp), c.recs)}
        res["op_same_forbidden_examples"] = [
            {"pair": k, "facts": facts(c.recs[k[0]], c.recs[k[1]]), "ladder_together": k in lcp}
            for k in sorted(pos) if facts(c.recs[k[0]], c.recs[k[1]])][:40]
        out[n] = res
        print(n, json.dumps({k: v for k, v in res.items() if k in ("op_same", "op_diff", "candidates",
                                                                  "ladder_full_copairs")}), flush=True)
    (OUT / f"facts_measure_{'_'.join(names)}.json").write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main(sys.argv[1:])
