"""C1: render the ablation arms of one cohort as a markdown table (and a compact JSON).

    python3 c1_table.py trial [c17 ...]
"""
import json
import pickle
import sys
from pathlib import Path

ROOT = Path("/home/hejtm/autodedup-artifacts/w15/census/c1")


def load(cohort):
    base = pickle.load(open(ROOT / "runs" / f"{cohort}_base.pkl", "rb"))[0]
    rows = []
    for line in (ROOT / "runs" / f"{cohort}_arms.jsonl").read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return base, rows


def fmt(cohort):
    base, rows = load(cohort)
    out = [f"### {cohort}: baseline merge {base['merge_pairs']}, band {base['zones'].get('band', 0)}, "
           f"groups {base['groups']}, co-pairs {base['copairs']}, operator-same together "
           f"{base['label_same_together']}/{base['label_same_total']}, operator-different together "
           f"{base['label_diff_together']}/{base['label_diff_total']}, Browse-merge yardstick "
           f"{base['yardstick_browse_together']}/{base['yardstick_browse_total']}"
           + (f", ref CD together {base['ref_cd_together']}/{base['ref_cd_total']}, ref CN together "
              f"{base['ref_cn_together']}/{base['ref_cn_total']}" if 'ref_cd_total' in base else ""),
           "",
           "| arm | Δmerge | Δband | Δgroups | co-pairs +/− | Δsame✓ | Δdiff✗ | Δbrowse | gained (labels) | lost (labels) |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    compact = {}
    for r in rows:
        if "invalid" in r:
            out.append(f"| {r['name']} | invalid: {r['invalid'][:80]} |||||||||")
            compact[r["name"]] = {"invalid": r["invalid"]}
            continue
        s, d = r["summary"], r["delta"]
        dband = s["zones"].get("band", 0) - base["zones"].get("band", 0)
        dsame = s["label_same_together"] - base["label_same_together"]
        ddiff = s["label_diff_together"] - base["label_diff_together"]
        dbrowse = s["yardstick_browse_together"] - base["yardstick_browse_together"]
        dgroups = s["groups"] - base["groups"]
        gl = {k: v for k, v in d["gained_labels"].items() if v}
        ll = {k: v for k, v in d["lost_labels"].items() if v}
        out.append(f"| {r['name']} | {d['merge_pairs_delta']:+d} | {dband:+d} | {dgroups:+d} | "
                   f"+{d['copairs_gained']}/−{d['copairs_lost']} | {dsame:+d} | {ddiff:+d} | "
                   f"{dbrowse:+d} | {gl or ''} | {ll or ''} |")
        compact[r["name"]] = {"dmerge": d["merge_pairs_delta"], "dband": dband, "dgroups": dgroups,
                              "cp_gained": d["copairs_gained"], "cp_lost": d["copairs_lost"],
                              "dsame": dsame, "ddiff": ddiff, "dbrowse": dbrowse,
                              "gained_labels": gl, "lost_labels": ll,
                              "zones": s["zones"], "conflicts": s["conflicts"],
                              "n_pairs": s["n_pairs"]}
    (ROOT / f"{cohort}_ablation.json").write_text(json.dumps({"base": base, "arms": compact},
                                                              indent=1, default=str))
    return "\n".join(out)


if __name__ == "__main__":
    for c in sys.argv[1:]:
        print(fmt(c))
        print()
