"""One calibration map for every cohort: isotonic on the TRIAL's out-of-town rows only (the one
balanced label set: 2,693 positive / 1,891 negative). Sealed for every other cohort; for the trial the
map sees its own labels, the ranking never does. Reuses a sealed run's raw out-of-town scores.

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.recalibrate <from tag> <to tag> <cohort> [...]
"""
from __future__ import annotations

import shutil
import sys

import numpy as np

from autodedup_w15_g2.fit import ground
from autodedup_w15_g2.learn import fit_isotonic, load_cohort
from autodedup_w15_g2.paths import OUT

KINDS = ("hgb", "lr", "mlp")
CARRY = ("w6_gold", "hgb-op", "hgb-op_c7", "hgb-judge")


def main(src: str, dst: str, names: list[str]) -> None:
    trial = ground(load_cohort("trial"), "none")
    for kind in KINDS:
        if not (OUT / f"cache/raw_{src}_trial_{kind}.npy").is_file():
            continue
        raw_t = np.load(OUT / f"cache/raw_{src}_trial_{kind}.npy")
        iso = fit_isotonic(raw_t[trial.idx], trial.y, trial.w)
        for n in names:
            raw = np.load(OUT / f"cache/raw_{src}_{n}_{kind}.npy")
            np.save(OUT / f"cache/p_{dst}_{n}_{kind}.npy", iso.predict(raw))
        print(kind, "levels", len(np.unique(iso.predict(raw_t))), flush=True)
    for key in CARRY:
        for n in names:
            if not (OUT / f"cache/p_{src}_{n}_{key}.npy").is_file():
                continue
            shutil.copy(OUT / f"cache/p_{src}_{n}_{key}.npy", OUT / f"cache/p_{dst}_{n}_{key}.npy")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3:])
