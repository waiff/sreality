"""Markdown tables of a sweep (one per cohort), optionally filtered to some rows.

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.sweep_table <tag> <cohort> [...] [--rows=a,b]
"""
from __future__ import annotations

import json
import sys

from autodedup_w15_g2.paths import OUT


def frac(m: dict, a: str, b: str) -> str:
    return f"{m[a]}/{m[b]}" if m.get(b) else "–"


def row(label: str, m: dict) -> str:
    adv = m.get("adversary", {})
    known = f"{sum(1 for v in adv.values() if not v['together'])}/{len(adv)}" if adv else "–"
    return (f"| {label} | {frac(m, 'op_same_together', 'op_same')} | {frac(m, 'op_diff_apart', 'op_diff')} | "
            f"{frac(m, 'c7_one_pairs_together', 'c7_one_pairs')} | {frac(m, 'c7_fused_pairs_apart', 'c7_fused_pairs')} | "
            f"{frac(m, 'judge_pos_together', 'judge_pos')} | {frac(m, 'judge_neg_apart', 'judge_neg')} | "
            f"{m['screen_flagged_groups']} ({m.get('screen_flagged_not_in_ladder', 0)}) | {m['band']:,} | "
            f"{m['groups']:,} | {m['copairs']:,} (+{m.get('copairs_gained_vs_ladder', 0):,}/"
            f"-{m.get('copairs_lost_vs_ladder', 0):,}) | {m['max_group']} | {known} |")


def main(tag: str, names: list[str]) -> None:
    only = next((a.split("=", 1)[1].split(",") for a in names if a.startswith("--rows=")), None)
    names = [a for a in names if not a.startswith("--")]
    for n in names:
        res = json.load(open(OUT / f"sweep_{tag}_{n}.json"))
        print(f"\n#### {n}\n")
        print("| engine | op same together | op different apart | C7 one-pairs together | C7 fused pairs apart | "
              "judge same together | judge different apart | screened groups (new vs R0) | band | groups | "
              "co-pairs (+/- vs R0) | max group | known fusions apart |")
        print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for arm, label in (("FULL", "ladder R0 (64 readers)"), ("FOLD7", "ladder, readers folded to 7 facts")):
            if arm in res["ladder"]:
                print(row(label, res["ladder"][arm]))
        for key, m in res["engine"].items():
            if only is None or key in only:
                print(row(f"MF {key}", m))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
