"""The label engine's first pair lists: where MF-H and the ladder disagree, MF-H's band, and the
pairs a stated fact blocks at a high score (a fact audit), minus every pair already labelled.
Written in the judge lane's `pairs_file` format (autodedup/pairs/<name>.json: {lo, hi, stratum}).

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.contested <cohort> [...]

Strata (seeded 20260927, caps per cohort): s1 ladder_only_edge (the ladder merges or promotes the
pair, MF-H does not co-cluster it) 300; s2 mf_only_edge (MF-H co-clusters, the ladder does not) 300;
s3 mf_band (0.2 <= p < 0.8, neither engine joins it) 250; s4 fact_block (p >= 0.8 and a fact fires)
100; s5 case_reader (origin cohorts only: a case reader is the SOLE reader firing, p >= 0.2) 400."""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import numpy as np

from autodedup_w15_g2.facts7 import first_fact
from autodedup_w15_g2.learn import load_cohort
from autodedup_w15_g2.metrics import copairs
from autodedup_w15_g2.paths import OUT
from autodedup_w15_g2.sweep import FACTS

REPO = Path(__file__).resolve().parents[1]
CAPS = {"s1_ladder_only_edge": 300, "s2_mf_only_edge": 300, "s3_mf_band": 250, "s4_fact_block": 100,
        "s5_case_reader": 400}
KEPT = {"category_type", "category_main", "area", "stated_area", "printed_area", "headline_area", "disposition",
        "floor", "total_floors", "price", "street", "unit_designator", "body_align", "interior", "floorplan",
        "plot_area"}


def main(names: list[str]) -> None:
    for n in names:
        c = load_cohort(n)
        p = np.load(OUT / f"cache/p_{'s8' if n == 'c5' else 's6'}_{n}_hgb.npy")[: c.n_cand]
        mf = {int(k): v for k, v in json.load(open(OUT / f"cache/groups_r2_{n}_MF-H.json")).items()}
        mcp, lcp = copairs(mf), copairs(c.ladder["FULL"]["clusters"])
        zone = c.ladder["FULL"]["zone"]
        labelled = set(c.labels)
        rng = random.Random(20260927)
        pools: dict[str, list] = {s: [] for s in CAPS}
        for i, k in enumerate(c.cand_keys):
            if k in labelled:
                continue
            f = first_fact(c.recs[k[0]], c.recs[k[1]], FACTS)
            if zone[i] == "merge" and k not in mcp and k in lcp:
                pools["s1_ladder_only_edge"].append(k)
            elif k in mcp and k not in lcp:
                pools["s2_mf_only_edge"].append(k)
            elif f is None and 0.2 <= p[i] < 0.8 and k not in mcp and k not in lcp:
                pools["s3_mf_band"].append(k)
            if f is not None and p[i] >= 0.8:
                pools["s4_fact_block"].append(k)
            if c.fired is not None and n not in ("trial", "c17", "c18") and p[i] >= 0.2:
                fired = set(c.fired[i])
                if len(fired) == 1 and not (fired & KEPT):
                    pools["s5_case_reader"].append(k)
        out, seen = [], set()
        sizes = {}
        for s, cap in CAPS.items():
            pick = [k for k in rng.sample(pools[s], min(cap, len(pools[s]))) if k not in seen]
            seen |= set(pick)
            sizes[s] = (len(pools[s]), len(pick))
            out += [{"lo": int(a), "hi": int(b), "stratum": f"g2:{s}"} for a, b in pick]
        path = REPO / f"autodedup/pairs/g2_contested_{n}.json"
        path.write_text(json.dumps(out, indent=0) + "\n")
        print(n, len(out), {s: f"{v[1]} of {v[0]}" for s, v in sizes.items()}, "->", path, flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
