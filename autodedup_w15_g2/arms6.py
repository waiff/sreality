"""arms5's reader sets, completed: the two image facts (interior, floorplan: same room tag, no
tight frame) alone and on top of the 14 text + case readers.

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.arms6 <tag> <cohort> [...]"""
from __future__ import annotations

import sys

from autodedup_w15_g2 import arms5

IMG = {"interior", "floorplan"}
arms5.SETS = {"+img2": IMG, "+text6+K10+img2": arms5.TEXT | arms5.K10 | IMG}

if __name__ == "__main__":
    arms5.main(sys.argv[1], sys.argv[2:])
