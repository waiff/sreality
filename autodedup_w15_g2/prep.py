"""One heavy pass per cohort (needs the export in memory): the ladder's FULL run and its
fold-to-seven-facts arms, the compact advert records, every label inside the cohort and the
features of the labelled pairs retrieval never produced. Everything after this is offline over
the pickles.

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.prep <cohort> [--no-arms]
"""
from __future__ import annotations

import json
import pickle
import resource
import sys
import time

import numpy as np

from autodedup_w15_g2 import ladder
from autodedup_w15_g2.facts7 import rec_of
from autodedup_w15_g2.labels_g2 import all_labels
from autodedup_w15_g2.paths import OUT

ALL_FACTS = set(json.load(open("/home/hejtm/autodedup-artifacts/w15/census/c1/fact_names.json")))
KEEP7 = {"category_type", "category_main", "area", "disposition", "floor", "price", "street",
         "unit_designator", "plot_area"}
ARMS = {
    "FULL": set(),
    "FOLD7": ALL_FACTS - KEEP7,
    "FOLD7_IMG": ALL_FACTS - KEEP7 - {"interior", "floorplan"},
}


def compact(run: ladder.Run) -> dict:
    return {
        "zone": np.array([d.zone for d in run.decisions]),
        "reason": [d.reason for d in run.decisions],
        "certificate": [d.certificate for d in run.decisions],
        "score": np.array([d.score if d.score is not None else np.nan for d in run.decisions]),
        "clusters": run.clusters,
        "stats": run.stats,
        "seconds": run.seconds,
    }


def main(name: str, arms: bool = True) -> None:
    clock = time.perf_counter()
    eng = ladder.Engine.build(name)
    recs = {lid: rec_of(l, eng.settings) for lid, l in eng.ds.listings.items()}
    labels = all_labels(name, recs)
    keyset = set(eng.keys)
    extra = sorted(k for k in labels if k not in keyset)
    Vx, Px = eng.features_for(extra)
    print(f"[{name}] labels {len(labels)} ({len(extra)} outside retrieval) "
          f"{time.perf_counter()-clock:.0f}s", flush=True)
    with open(OUT / f"cache/prep_{name}.pkl", "wb") as handle:
        pickle.dump({"keys": eng.keys, "V": eng.V, "P": eng.P, "extra": extra, "Vx": Vx, "Px": Px,
                     "recs": recs, "labels": labels}, handle, protocol=5)
    out = {}
    for arm, disabled in ARMS.items():
        if arm != "FULL" and not arms:
            continue
        run = eng.run(disabled)
        out[arm] = compact(run)
        print(f"[{name}] {arm}: {sum(1 for d in run.decisions if d.zone == 'merge')} merges, "
              f"{len(run.clusters)} groups, {run.seconds:.0f}s, "
              f"rss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f} MB", flush=True)
        with open(OUT / f"cache/ladder_{name}.pkl", "wb") as handle:
            pickle.dump(out, handle, protocol=5)
    print(f"[{name}] done {time.perf_counter()-clock:.0f}s", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], "--no-arms" not in sys.argv)
