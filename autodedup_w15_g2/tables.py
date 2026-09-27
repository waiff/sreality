"""The G2 report tables (markdown) from a sealed run and its final engine arms.

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.tables <tag> <cohort> [...]
"""
from __future__ import annotations

import json
import sys

from autodedup_w15_g2.paths import OUT

ROWS = [("ladder", "FULL", "ladder R0 (w31 + w6_gold, 64 readers)"),
        ("ladder", "FOLD7", "ladder, readers folded to the 7 facts"),
        ("final", "w6_gold:f7sa+ba@0.9788", "MF, score = w6_gold as shipped"),
        ("final", "lr:f7sa+ba@0.9", "MF, logistic refit + isotonic"),
        ("final", "mlp:f7sa+ba@0.9", "MF, MLP 64-32"),
        ("final", "hgb:f7@0.9", "MF, boosting, loose facts"),
        ("final", "hgb:f7sa+ba@0.8", "MF, boosting, t 0.80"),
        ("final", "hgb:f7sa+ba@0.9", "**MF, boosting, t 0.90 (headline)**"),
        ("final", "hgb:f7sa+ba@0.95", "MF, boosting, t 0.95"),
        ("final", "hgb-op:f7sa+ba@0.9", "MF, trained on operator rulings only"),
        ("final", "hgb-op_c7:f7sa+ba@0.9", "MF, operator + C7 reads"),
        ("final", "hgb-judge:f7sa+ba@0.9", "MF, judge labels only")]


def frac(m: dict, a: str, b: str) -> str:
    return f"{m[a]}/{m[b]}" if m.get(b) else "–"


def main(tag: str, names: list[str]) -> None:
    src = next((a.split("=", 1)[1] for a in names if a.startswith("--sealed=")), tag)
    names = [a for a in names if not a.startswith("--")]
    sealed = json.load(open(OUT / f"sealed_{src}.json"))
    final = json.load(open(OUT / f"engine_final_{tag}_{'_'.join(names)}.json"))
    for n in names:
        res = sealed["cohorts"][n]
        print(f"\n#### {n}: {res['adverts']:,} adverts, {res['candidates']:,} candidate pairs, "
              f"{res['towns']} towns, {res['labels']:,} labels in the cohort "
              f"(+{res['labels_pos']:,} / -{res['labels_neg']:,})\n")
        print("| engine | op same together | op different apart | C7 one-pairs together | "
              "C7 fused pairs apart | judge neg apart | screened groups (new vs R0) | band | "
              "groups | co-pairs (+/- vs R0) | known fusions apart |")
        print("|---|---|---|---|---|---|---|---|---|---|---|")
        for src, key, label in ROWS:
            m = res["ladder"].get(key) if src == "ladder" else final.get(n, {}).get(key)
            if m is None:
                continue
            adv = m.get("adversary", {})
            known = f"{sum(1 for v in adv.values() if not v['together'])}/{len(adv)}" if adv else "–"
            print(f"| {label} | {frac(m, 'op_same_together', 'op_same')} | "
                  f"{frac(m, 'op_diff_apart', 'op_diff')} | "
                  f"{frac(m, 'c7_one_pairs_together', 'c7_one_pairs')} | "
                  f"{frac(m, 'c7_fused_pairs_apart', 'c7_fused_pairs')} | "
                  f"{frac(m, 'judge_neg_apart', 'judge_neg')} | "
                  f"{m['screen_flagged_groups']} ({m.get('screen_flagged_not_in_ladder', 0)}) | "
                  f"{m['band']:,} | {m['groups']:,} | {m['copairs']:,} "
                  f"(+{m.get('copairs_gained_vs_ladder', 0):,}/-{m.get('copairs_lost_vs_ladder', 0):,}) | "
                  f"{known} |")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
