"""Tables for C2_measurements.md from the JSON the other c2 scripts wrote (any subset)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import c2lib  # noqa: E402
from c2lib import FAMILY_OF, FEATURE_ORDER  # noqa: E402
from autodedup.harness import load_model  # noqa: E402

OUT = Path("/home/hejtm/autodedup-artifacts/w15/census/c2")
COHORTS = ("trial", "c17", "c18")


def load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def arm(cohort: str, name: str):
    return load(OUT / f"arms_{cohort}" / f"{name}.json")


def delta_cell(r) -> str:
    if r is None:
        return "-"
    if "error" in r:
        return "refused"
    d = r["merge"] - r["merge_base"]
    ops = r["op_same_together"] - r["op_same_together_base"]
    opd = r["op_diff_together"] - r["op_diff_together_base"]
    opm = r["op_merge_pairs_together"] - r["op_merge_pairs_together_base"]
    return (f"{d:+d} merges, {r['merge_flips']} flips ({100*r['merge_flip_share']:.2f}%), "
            f"groups +{r['groups_differing']}/-{r.get('groups_base_only', 0)}, copairs +{r['copairs_gained']}/-{r['copairs_lost']}, "
            f"op same {ops:+d}, op diff {opd:+d}, yardstick {opm:+d}")


def short_cell(r) -> str:
    if r is None:
        return "-"
    if "error" in r:
        return "refused"
    d = r["merge"] - r["merge_base"]
    ops = r["op_same_together"] - r["op_same_together_base"]
    opd = r["op_diff_together"] - r["op_diff_together_base"]
    return (f"{d:+d} / {r['merge_flips']} / +{r['groups_differing']}-{r.get('groups_base_only', 0)} / "
            f"{ops:+d} / {opd:+d}")


def main() -> None:
    model = load_model(str(c2lib.MODEL))
    lines: list[str] = []
    red = {c: load(OUT / f"redundancy_{c}.json") for c in COHORTS}
    # --- feature table
    import c2_meta
    lines.append("| # | feature | fam | measures | w | pw | pres c17 | logodds SD c17 (stored region) | consumers outside the model | single mean-ablation trial: dMerge/flips/groups/opSame/opDiff | c17 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for i, name in enumerate(FEATURE_ORDER):
        w = model.weights.get(name)
        pw = model.presence_weights.get(name)
        f17 = (red.get("c17") or red.get("trial") or {}).get("features", {}).get(name, {})
        lines.append(
            f"| {i} | {name} | {FAMILY_OF[name]} | {c2_meta.MEANS[name]} | {'' if w is None else f'{w:+.4f}'} | "
            f"{'' if pw is None else f'{pw:+.4f}'} | {f17.get('present_share', '')} | "
            f"{f17.get('logodds_sd_region', '')} | {c2_meta.CONSUMERS[name]} | {short_cell(arm('trial', 'single_' + name))} | "
            f"{short_cell(arm('c17', 'single_' + name))} |")
    lines.append("")
    # --- family table
    fams = ["ATTR", "PRICE", "TXT", "BRK", "LOC", "IMG", "TIME", "IMG_DHASH", "IMG_CLIP",
            "IMG_FAMRATIO", "IMG_TAG", "IMG_META", "IMG_TAG+FAMRATIO", "IMG_CLIPALL"]
    lines.append("| family | model mean-ablation trial | model c17 | silence everywhere trial | silence c17 |")
    lines.append("|---|---|---|---|---|")
    for fam in fams:
        lines.append(f"| {fam} | {short_cell(arm('trial', 'model_' + fam))} | "
                     f"{short_cell(arm('c17', 'model_' + fam))} | "
                     f"{short_cell(arm('trial', 'silence_' + fam))} | "
                     f"{short_cell(arm('c17', 'silence_' + fam))} |")
    lines.append("")
    for name in ("const_reject", "const_band", "e11_off", "context_rule_off", "e61_veto_off",
                 "auto_reject_off", "auto_reject_attr_off", "auto_reject_numeral_off",
                 "silence_ref_code_shared"):
        lines.append(f"- {name}: trial {delta_cell(arm('trial', name))}; c17 {delta_cell(arm('c17', name))}")
    lines.append("")
    # --- readers
    cen = {c: load(OUT / f"readers_{c}" / "census.json") for c in COHORTS}
    import c2_meta
    hist = {}
    for hp in sorted((OUT / "history").glob("*.json")):
        h = load(hp)
        if h:
            hist[h["cohort"]] = h
    lines.append("| # | reader | code | predicate / tolerance | repeats | trial g/p/c fires (sole) | c17 | c18 | cohorts 3-16 g/p/c fires (n cohorts) | KO trial dMerge/flips/groups/opSame/opDiff | KO c17 | KO c18 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    import c2_readers
    for idx, r in enumerate(c2_readers.READERS, 1):
        code, tol, rep = c2_meta.READER_META[r]
        hg = sum(h["fires"]["gate"].get(r, 0) for h in hist.values())
        hp_ = sum(h["fires"]["promote"].get(r, 0) for h in hist.values())
        hc = sum(h["fires"]["cluster"].get(r, 0) for h in hist.values())
        hn = sum(1 for h in hist.values() if any(h["fires"][m].get(r) for m in ("gate", "promote", "cluster")))
        hcell = f"{hg}/{hp_}/{hc} ({hn}/{len(hist)})" if hist else "-"
        cells = []
        for c in COHORTS:
            v = ((cen.get(c) or {}).get("readers") or {}).get(r)
            if v is None:
                cells.append("-")
                continue
            cells.append(
                f"{v.get('decide_gate_fires', 0)}({v.get('decide_gate_sole', 0)}) / "
                f"{v.get('decide_promote_fires', 0)}({v.get('decide_promote_sole', 0)}) / "
                f"{v.get('cluster_cluster_fires', 0)}({v.get('cluster_cluster_sole', 0)})")
        kos = [short_cell(load(OUT / f"readers_{c}" / f"ko_{r}.json")) for c in COHORTS]
        lines.append(f"| {idx} | {r} | {code} | {tol} | {rep} | {' | '.join(cells)} | {hcell} | {' | '.join(kos)} |")
    lines.append("")
    for name in list(c2_readers.SETTINGS_ARMS) + ["demo_area_off", "demo_price_off", "d50_pair_off"]:
        cells = [delta_cell(load(OUT / f"readers_{c}" / f"{name}.json")) for c in COHORTS]
        lines.append(f"- {name}: trial {cells[0]}; c17 {cells[1]}; c18 {cells[2]}")
    (OUT / "c2_tables.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
