"""Rebuild section 9 (appendices A-C) of C2_measurements.md from the generated tables."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
OUT = Path("/home/hejtm/autodedup-artifacts/w15/census/c2")
DOC = OUT.parent / "C2_measurements.md"


def main() -> None:
    subprocess.run([sys.executable, str(HERE / "c2_report.py")], capture_output=True, check=True)
    blocks = (OUT / "c2_tables.md").read_text().split("\n\n")
    feat = next(b for b in blocks if b.startswith("| # | feature"))
    reader = next(b for b in blocks if b.startswith("| # | reader"))
    ov = json.loads((OUT / "overfit.json").read_text())
    lines = ["| reader | dev fires / sole (cohorts firing of 14) | sole per 10k dev | unseen fires / sole | "
             "sole per 10k unseen | top dev cohort (share of dev sole) |", "|---|---|---|---|---|---|"]
    for x in ov["readers"]:
        lines.append(f"| {x['reader']} | {x['dev_fires']} / {x['dev_sole']} ({x['dev_cohorts_firing']}) | "
                     f"{x['dev_sole_per_10k']} | {x['new_fires']} / {x['new_sole']} | {x['new_sole_per_10k']} | "
                     f"{x['top_dev_cohort']} ({x['top_dev_share']}) |")
    app = "\n".join([
        "## 9. Appendices", "",
        "### Appendix A - the 60 features", "",
        "Columns: index in FEATURE_ORDER, name, FAMILY_OF, what it measures, w6_gold value weight w and presence "
        "weight pw, presence share on cohort 17, SD of the feature's log-odds contribution on cohort 17's stored "
        "region (score >= 0.02; interactions split half to each side), live consumers outside the model "
        "(\"(off)\" = switched off in w31), single-feature mean ablation (dMerge / flips / groups +arm-only-base-only "
        "/ d op same / d op diff) on trial and cohort 17.", "", feat, "",
        "### Appendix B - the 64 D43 readers", "",
        "Columns: order in distinguishing_facts, name, code line, predicate / tolerance, what else in the engine "
        "compares the same fact, firings per reading gate / promote / cluster with (sole) on trial, cohort 17 and "
        "cohort 18 (FULL, real populations, with feature slots), firings on the 14 dev cohorts 3-16 (history census, "
        "feats-less populations, 8.2; n = cohorts firing), knockout delta (dMerge / flips / groups +a-b / d op same "
        "/ d op diff) on trial and cohort 17 (cohort 18 ran census-only).", "", reader, "",
        "### Appendix C - dev vs unseen firing, every reader (overfit.json)", "", "\n".join(lines), ""])
    doc = DOC.read_text()
    head = doc.split("## 9. Appendices")[0].rstrip() + "\n\n"
    DOC.write_text(head + app)
    print(len(head), len(app))


if __name__ == "__main__":
    main()
