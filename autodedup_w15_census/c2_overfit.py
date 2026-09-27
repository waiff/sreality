"""Dev-vs-unseen firing of every D43 reader on the SAME populations (c2_history): the 14 cohorts
the readers were written on (3-16) against the three cohorts none was written on (trial, 17, 18).
Rates are sole firings (the reader is the only fact on the pair) per 10,000 adverts."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import c2_readers  # noqa: E402

OUT = Path("/home/hejtm/autodedup-artifacts/w15/census/c2")
UNSEEN = ("trial", "c17", "c18")


def main() -> None:
    hist = {}
    for path in sorted((OUT / "history").glob("*.json")):
        h = json.loads(path.read_text())
        hist[h["cohort"]] = h
    dev = {k: v for k, v in hist.items() if k not in UNSEEN}
    new = {k: v for k, v in hist.items() if k in UNSEEN}
    n_dev = sum(h["n_listings"] for h in dev.values())
    n_new = sum(h["n_listings"] for h in new.values())
    rows = []
    for r in c2_readers.READERS:
        def tot(group, kind):
            return sum(h[kind][m].get(r, 0) for h in group.values() for m in ("gate", "promote", "cluster"))
        sd, sn = tot(dev, "sole"), tot(new, "sole")
        fd, fn = tot(dev, "fires"), tot(new, "fires")
        per = sorted(((sum(h["sole"][m].get(r, 0) for m in ("gate", "promote", "cluster")), k)
                      for k, h in dev.items()), reverse=True)
        top_share = (per[0][0] / sd) if sd else 0.0
        rows.append({"reader": r, "dev_fires": fd, "dev_sole": sd, "new_fires": fn, "new_sole": sn,
                     "dev_sole_per_10k": round(1e4 * sd / n_dev, 2) if n_dev else None,
                     "new_sole_per_10k": round(1e4 * sn / n_new, 2) if n_new else None,
                     "top_dev_cohort": per[0][1] if sd else None,
                     "top_dev_share": round(top_share, 3),
                     "dev_cohorts_firing": sum(1 for h in dev.values() if any(h["fires"][m].get(r) for m in ("gate", "promote", "cluster")))})
    report = {"dev_cohorts": sorted(dev), "dev_listings": n_dev, "unseen_cohorts": sorted(new),
              "unseen_listings": n_new, "readers": rows}
    (OUT / "overfit.json").write_text(json.dumps(report, indent=1))
    print(f"dev {len(dev)} cohorts {n_dev} adverts; unseen {sorted(new)} {n_new} adverts")
    print("| reader | dev fires / sole (cohorts) | sole per 10k dev | unseen fires / sole | sole per 10k unseen | top dev cohort (share of dev sole) |")
    print("|---|---|---|---|---|---|")
    for x in rows:
        print(f"| {x['reader']} | {x['dev_fires']} / {x['dev_sole']} ({x['dev_cohorts_firing']}/{len(dev)}) | "
              f"{x['dev_sole_per_10k']} | {x['new_fires']} / {x['new_sole']} | {x['new_sole_per_10k']} | "
              f"{x['top_dev_cohort']} ({x['top_dev_share']}) |")


if __name__ == "__main__":
    main()
