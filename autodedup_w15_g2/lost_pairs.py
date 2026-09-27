"""Every co-pair the ladder forms and MF-H does not, by the MF-H cause; a seeded sample for a read.

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.lost_pairs <groups tag> <cohort> <n>"""
from __future__ import annotations

import collections
import json
import random
import sys

import numpy as np

from autodedup.dataset import load

from autodedup_w15_g2.facts7 import facts
from autodedup_w15_g2.learn import load_cohort
from autodedup_w15_g2.metrics import copairs
from autodedup_w15_g2.paths import COHORTS, OUT
from autodedup_w15_g2.read_kit import advert_line
from autodedup_w15_g2.sweep import FACTS


def main(tag: str, name: str, n: int) -> None:
    c = load_cohort(name)
    mf = {int(k): v for k, v in json.load(open(OUT / f"cache/groups_{tag}_{name}_MF-H.json")).items()}
    p = np.load(OUT / f"cache/p_{'s8' if name == 'c5' else 's6'}_{name}_hgb.npy")
    pos = {k: i for i, k in enumerate(c.keys)}
    lost = sorted(copairs(c.ladder["FULL"]["clusters"]) - copairs(mf))
    cause = collections.Counter()
    bucket = collections.defaultdict(list)
    for k in lost:
        f = facts(c.recs[k[0]], c.recs[k[1]], FACTS)
        i = pos.get(k)
        if f:
            cz = f"fact:{f[0]}"
        elif i is None:
            cz = "not_candidate (ladder transitivity)"
        elif p[i] < 0.2:
            cz = "p<0.2"
        elif p[i] < 0.8:
            cz = "0.2<=p<0.8 (band)"
        else:
            cz = "p>=0.8, refused or elsewhere at group grain"
        cause[cz] += 1
        bucket[cz].append(k)
    print(name, len(lost), dict(cause.most_common()))
    L = load(str(COHORTS[name])).listings
    where = {m: r for r, ms in mf.items() for m in ms}
    rng = random.Random(20260927)
    out = []
    for cz in ("0.2<=p<0.8 (band)", "p<0.2", "p>=0.8, refused or elsewhere at group grain"):
        for a, b in rng.sample(bucket[cz], min(n, len(bucket[cz]))):
            i = pos.get((a, b))
            out.append(f"\n## {cz}: {a} x {b} p={round(float(p[i]), 3) if i is not None else None}")
            out.append(advert_line(a, L[a], where))
            out.append(advert_line(b, L[b], where))
    (OUT / f"lost_pairs_{tag}_{name}.txt").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]))
