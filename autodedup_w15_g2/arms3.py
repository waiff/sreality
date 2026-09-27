"""The street limb of the place fact, measured on the headline (a corner house has two streets).

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.arms3 <tag> <cohort> [...]"""
from __future__ import annotations

import dataclasses
import sys

from autodedup_w15_g2 import arms2
from autodedup_w15_g2.sweep import FACTS

NOSTREET = dataclasses.replace(FACTS, street=False)
arms2.ARMS = [("MF-H", "s6", "hgb", 0.8, 0.2, FACTS), ("MF-H-nostreet", "s6", "hgb", 0.8, 0.2, NOSTREET)]

if __name__ == "__main__":
    arms2.main(sys.argv[1], sys.argv[2:])
