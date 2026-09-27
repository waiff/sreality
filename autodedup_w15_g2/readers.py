"""The 64 fact readers as FEATURES, not rules: which readers fire on each pair under the group
(CLUSTER) reading, recorded as data for the learned score. No excuse, dial or reading order is
kept by the model-first engine; the model learns what each reader's firing is worth.

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.readers <cohort>
"""
from __future__ import annotations

import json
import pickle
import sys
import time

from autodedup import indistinguishable as ind
from autodedup.dataset import load
from autodedup.settings import Settings

from autodedup_w15_g2.ladder import SETTINGS, _ORIGINAL_DF, feats_row
from autodedup_w15_g2.paths import COHORTS, OUT

NAMES: list[str] = sorted(json.load(open("/home/hejtm/autodedup-artifacts/w15/census/c1/fact_names.json")))


def main(name: str) -> None:
    clock = time.perf_counter()
    with open(OUT / f"cache/prep_{name}.pkl", "rb") as handle:
        prep = pickle.load(handle)
    ds = load(str(COHORTS[name]))
    settings = Settings.from_json(SETTINGS)
    rows = [(prep["keys"], prep["V"], prep["P"]), (prep["extra"], prep["Vx"], prep["Px"])]
    fired: list[tuple[str, ...]] = []
    for keys, V, P in rows:
        for i, (lo, hi) in enumerate(keys):
            facts = _ORIGINAL_DF(ds.listings[lo], ds.listings[hi], feats_row(V, P, i), settings,
                                 ind.CLUSTER)
            fired.append(tuple(sorted({f.name for f in facts})))
            if len(fired) % 50000 == 0:
                print(f"[{name}] {len(fired)} {time.perf_counter()-clock:.0f}s", flush=True)
    with open(OUT / f"cache/readers_{name}.pkl", "wb") as handle:
        pickle.dump({"names": NAMES, "fired": fired}, handle, protocol=5)
    print(f"[{name}] readers on {len(fired)} rows in {time.perf_counter()-clock:.0f}s", flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
