"""Section-8 tables: cohort 18 base / census, combined deletion arms on trial / c17 / c18, the
history census summary for the zero-effect readers."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import c2_readers  # noqa: E402

OUT = Path("/home/hejtm/autodedup-artifacts/w15/census/c2")


def load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def cell(r) -> str:
    if r is None:
        return "-"
    return (f"{r['merge_base']:,} -> {r['merge']:,} ({r['merge']-r['merge_base']:+d}); flips {r['merge_flips']} "
            f"({100*r['merge_flip_share']:.2f} %); groups {r['groups_base']:,} -> {r['groups']:,} "
            f"(+{r['groups_differing']}-{r.get('groups_base_only', 0)}); copairs +{r['copairs_gained']}"
            f"-{r['copairs_lost']}; op same {r['op_same_together_base']}->{r['op_same_together']} of "
            f"{r['op_same_n']}; op diff {r['op_diff_together_base']}->{r['op_diff_together']} of "
            f"{r['op_diff_n']}; yardstick {r['op_merge_pairs_together_base']}->"
            f"{r['op_merge_pairs_together']} of {r['op_merge_pairs_n']}")


def main() -> None:
    out = []
    b18 = load(OUT / "base_c18.json")
    if b18:
        v = b18.get("verify", {})
        out.append(f"cohort 18 FULL: {b18['n_listings']:,} adverts, {b18['pairs']:,} pairs, zones {b18['zones']}, "
                   f"groups {b18['groups']:,}; verify {v.get('identical')}/{v.get('stored_rows')} identical "
                   f"(zone {v.get('zone_differs')}, reason {v.get('reason_differs')}, score {v.get('score_differs')}); "
                   f"operator labels {b18['operator']} together {b18['operator_together']}")
    c18 = load(OUT / "readers_c18" / "census.json")
    if c18:
        out.append(f"cohort 18 census: evaluated {c18['evaluated_pairs']} nonempty {c18['evaluated_nonempty']}; "
                   f"never fired ({len(c18['never_fired'])}): {c18['never_fired']}; reasons {c18['decision_reasons']}")
    for name in ("const_band", "const_reject"):
        out.append(f"c18 {name}: {cell(load(OUT / 'arms_c18' / f'{name}.json'))}")
    spec = load(OUT / "combined_spec.json") or {}
    for arm in spec:
        for cohort in ("trial", "c17", "c18"):
            out.append(f"combined {arm} [{cohort}]: {cell(load(OUT / f'combined_{cohort}' / f'{arm}.json'))}")
    hist = {}
    for path in sorted((OUT / "history").glob("*.json")):
        h = load(path)
        if h:
            hist[h["cohort"]] = h
    cen = {c: load(OUT / f"readers_{c}" / "census.json") for c in ("trial", "c17", "c18")}
    never_all = [r for r in c2_readers.READERS
                 if all(c and not (c["readers"].get(r)) for c in cen.values() if c)]
    out.append(f"never fired on trial+c17+c18 ({len(never_all)}): {never_all}")
    rows = []
    for r in never_all:
        per = {k: sum(h["fires"][m].get(r, 0) for m in ("gate", "promote", "cluster")) for k, h in hist.items()}
        sole = {k: sum(h["sole"][m].get(r, 0) for m in ("gate", "promote", "cluster")) for k, h in hist.items()}
        n = sum(1 for v in per.values() if v)
        rows.append(f"| {r} | {n}/{len(hist)} | {sum(per.values())} | {sum(sole.values())} | "
                    f"{', '.join(f'{k}:{v}' for k, v in per.items() if v)} |")
    out.append("| reader | cohorts firing (3-16) | firings | sole | where |")
    out.append("|---|---|---|---|---|")
    out.extend(rows)
    text = "\n".join(out)
    (OUT / "c2_final.md").write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
