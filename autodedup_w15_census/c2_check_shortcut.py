"""Proof that the silence arms' shortcut equals a full re-decide (trial, two families)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import c2lib  # noqa: E402
import c2_arms  # noqa: E402
from c2lib import ABSENT  # noqa: E402

OUT = Path("/home/hejtm/autodedup-artifacts/w15/census/c2")


def main() -> None:
    eng = c2lib.Engine.build("trial", OUT / "cache")
    eng.baseline()
    report = {}
    for fam in ("ATTR", "IMG_TAG", "TXT"):
        names = set(c2_arms.FAMILIES[fam])

        def transform(f, names=names):
            g = dict(f)
            for n in names:
                g[n] = ABSENT
            return g

        _, full = eng.decide_all(eng.model, eng.settings, transform=transform)
        short = c2_arms.run_silence_arm(eng, names, None)
        flips_full = [[a.lo, a.hi, a.zone, a.reason, b.zone, b.reason]
                      for a, b in zip(eng.base.decisions, full) if a.zone != b.zone][:400]
        report[fam] = {
            "full_decisions_changed": sum(1 for a, b in zip(eng.base.decisions, full)
                                          if a.zone != b.zone or a.reason != b.reason),
            "short_decisions_changed": short["decisions_changed"],
            "flip_lists_equal": flips_full == short["flipped_pairs"],
        }
        print(fam, report[fam], flush=True)
    (OUT / "shortcut_check.json").write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
