"""The D83-style read list for the model-first engine: groups it forms that the ladder does not and
that C7's fused-shape screen flags, seeded sample, written as a groups file for `cards`.

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.sample_read <tag> <cohort> <n>
"""
from __future__ import annotations

import json
import random
import sys

from autodedup_w15_g2.learn import load_cohort
from autodedup_w15_g2.metrics import group_sets, screened
from autodedup_w15_g2.paths import OUT


def main(tag: str, name: str, n: int) -> None:
    c = load_cohort(name)
    groups = {int(k): v for k, v in json.load(open(OUT / f"cache/groups_{tag}_{name}_hgb.json")).items()}
    ladder = group_sets(c.ladder["FULL"]["clusters"])
    mine = group_sets(groups)
    for side, flagged in (("mf", screened(groups, c.recs) - ladder),
                          ("ladder", screened(c.ladder["FULL"]["clusters"], c.recs) - mine)):
        new = sorted((sorted(g) for g in flagged), key=lambda g: (-len(g), g))
        pick = random.Random(20260927).sample(new, min(n, len(new)))
        path = OUT / f"read_{tag}_{name}_{side}.json"
        path.write_text(json.dumps(pick))
        sizes = sorted((len(g) for g in new), reverse=True)
        print(f"{name} {side}-only screened groups: {len(new)}, sizes {sizes[:12]}; sample {len(pick)} -> {path}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]))
