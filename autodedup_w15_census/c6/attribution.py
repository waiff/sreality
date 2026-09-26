"""C6 prototype: ONE table per generation of the measurement families responsible for decisions.

    python3 attribution.py <run_dir> <cohort.jsonl.gz> <out_dir> [--settings w31] [--model w6_gold]

Reads a harness/score run directory (pairs.jsonl.gz + clusters.json + run.json) and the cohort
export it was run on; writes <out_dir>/attribution.json and attribution.md. Offline, read-only.

DEFINITION (every stored decision gets exactly ONE (rung, family) cell):
  rung   = the ladder step that set the final zone, read from the decision's own reason string
  family = the ONE measurement family (features.FAMILY_OF vocabulary: ATTR PRICE TXT BRK LOC IMG
           TIME, plus OPERATOR for rulings and NONE where nothing carried it) that carried it:
    certificate      K-R -> TXT (order code), K-B -> TXT (contained body re-post), K-C -> IMG
    model merge      argmax_f of the POSITIVE log-odds sum of family f under the fitted weights
                     (LogisticModel.contributions: w * standardised value + presence term; an
                     interaction term is split half/half between its two features' families),
                     over PRESENT features only: an absent feature's term is MISSING (E12)
    model band/reject argmin_f (the family that pulled the score DOWN most)
    d43_gate         the family of the first distinguishing fact (reader -> family table below)
    d43_promote      the (B) unit-grade corroboration that demonstrate.corroboration_warrant
                     names (photo IMG, body TXT, code TXT, twin TXT, pin LOC, price_path PRICE);
                     warrant photo / unit:photos -> IMG, unit:code / unit:body -> TXT
    d43_demonstrate  A:<fact> -> the fact's family; B -> NONE (no unit-grade evidence)
    auto_reject      attr_contradictions -> ATTR, numeral_conflict -> TXT
    guard            unit_designator_conflict -> TXT; walls (area/dispo/floor/category) -> ATTR
    evidence_pending (lane F3 hold) -> IMG
ADVERT GRAIN: an advert in a group of >= 2 takes the cell of its best in-group incident merge
edge under cluster.edge_rank (certificate first, score desc, lo, hi); a member with no such edge
is OPERATOR/must_link. An advert in no group takes: cluster_refused (a merge edge the group
invariants refused; family from the refusing fact) > its best band pair > its best stored
reject/veto > no_stored_pair (never compared, walled, or every pair under the store floor).
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import math
import sys
from pathlib import Path

WT = "/home/hejtm/dev/sreality/.claude/worktrees/w15-c6"
sys.path.insert(0, WT)
from autodedup.dataset import load  # noqa: E402
from autodedup.decide import stratum_t_hi  # noqa: E402
from autodedup.demonstrate import corroboration_warrant  # noqa: E402
from autodedup.features import FAMILY_OF  # noqa: E402
from autodedup.d43 import ClusterRelation  # noqa: E402
from autodedup.indistinguishable import CLUSTER, distinguishing_facts  # noqa: E402
from autodedup.model import LogisticModel, sigmoid  # noqa: E402
from autodedup.settings import Settings  # noqa: E402

FAMILIES = ("ATTR", "PRICE", "TXT", "BRK", "LOC", "IMG", "TIME", "MISSING", "OPERATOR", "NONE", "GROUP")
CERT_FAMILY = {"K-R": "TXT", "K-B": "TXT", "K-C": "IMG", "K-A": "LOC"}
CORROBORATION_FAMILY = {"photo": "IMG", "body": "TXT", "code": "TXT", "twin": "TXT",
                        "pin": "LOC", "price_path": "PRICE", "outside_development": "NONE",
                        "off": "NONE"}
_ATTR = """category_type category_main area stated_area printed_area headline_area offer_area
two_unit accessory accessory_area cellar_area outdoor_accessory extent extent_package
extent_variant part_whole part_addition disposition unit_count floor total_floors prose_floor
subject_floor storey_word offered_storey plot_area plot_prose plot_prose_exact plot_attribute
neighbour_plot product_class offered_use commercial_subtype rental_colive onesided_area
onesided_floor"""
_PRICE = "price charge accessory_price priced_row"
_LOC = """obec obec_prose body_obec street street_prose orientation stored_house_number
printed_house_number parcel"""
_TXT = """unit_designator printed_designator unit_code labelled_unit english_unit_code slug_unit
space_number plan_space position_designator named_villa lot_label agency_code agency_code_plus
agency_code_colive body_align onesided_code unit_designator_conflict numeral_conflict"""
_IMG = "interior floorplan"
FACT_FAMILY: dict[str, str] = {}
for fam, names in (("ATTR", _ATTR), ("PRICE", _PRICE), ("LOC", _LOC), ("TXT", _TXT),
                   ("IMG", _IMG)):
    for n in names.split():
        FACT_FAMILY[n] = fam
FACT_FAMILY["attr_contradictions"] = "ATTR"


def fact_family(name: str) -> str:
    return FACT_FAMILY.get(name, "UNMAPPED:" + name)


def feats_of(raw: dict) -> dict[str, tuple[float, bool]]:
    out = {}
    for k, v in (raw or {}).items():
        if isinstance(v, (list, tuple)):
            out[k] = (float(v[0]), bool(v[1]) if len(v) > 1 else True)
        else:
            out[k] = (float(v), True)
    return out


def family_sums(model: LogisticModel, feats) -> dict[str, float]:
    """Log-odds per family over PRESENT features; an absent feature's term (value 0 standardised
    plus its presence weight) is MISSING, never evidence of its family (E12)."""
    sums: dict[str, float] = collections.defaultdict(float)
    for name, value in model.contributions(feats).items():
        parts = name.split("*", 1) if "*" in name else [name.split(":")[0]]
        for base in parts:
            present = feats.get(base, (0.0, False))[1]
            sums[FAMILY_OF.get(base, "NONE") if present else "MISSING"] += value / len(parts)
    return dict(sums)


def calibrated(model: LogisticModel, logit: float) -> float:
    return model.apply_calibration(sigmoid(logit))


def classify(row, feats, model, settings, listings):
    """(rung, family, detail) for one stored decision."""
    zone, reason = row["zone"], row.get("reason") or ""
    parts = reason.split(":")
    cert = row.get("certificate")
    if reason.startswith("guard:"):
        return "guard", fact_family(parts[1]) if parts[1] in FACT_FAMILY else "ATTR", parts[1]
    if reason.startswith("auto_reject:"):
        return "auto_reject", fact_family(parts[1]), parts[1]
    if "d43_gate" in parts:
        fact = parts[parts.index("d43_gate") + 1]
        return ("d43_gate<" + (parts[1] if parts[0] == "certificate" else "model") + ">",
                fact_family(fact), fact)
    if "d43_demonstrate" in parts:
        i = parts.index("d43_demonstrate")
        if parts[i + 1] == "A":
            return "d43_refused:A", fact_family(parts[i + 2]), parts[i + 2]
        return "d43_refused:B", "NONE", "B"
    if reason.startswith("d43_promote:"):
        warrant = ":".join(parts[1:])
        if warrant == "photo" or warrant.startswith("unit:photos"):
            return "d43_promote<" + warrant.split(":")[0] + ">", "IMG", warrant
        if warrant in ("unit:code", "unit:body"):
            return "d43_promote<unit>", "TXT", warrant
        la, lb = listings.get(row["lo"]), listings.get(row["hi"])
        found = corroboration_warrant(la, lb, feats, settings) if la and lb else None
        return "d43_promote<agree>", CORROBORATION_FAMILY.get(found or "off", "NONE"), str(found)
    if reason.startswith("certificate:"):
        return ("certificate<" + parts[1] + ">", CERT_FAMILY.get(parts[1], "NONE"),
                reason if len(parts) > 2 else parts[1])
    if reason.startswith("evidence_pending"):
        return "hold<evidence_pending>", "IMG", reason
    if reason.startswith("context_rule"):
        return "context_rule", "NONE", reason
    if "policy_hold" in parts:
        return "policy_hold", "NONE", reason
    if parts[0] in ("model", "evidence_gate"):
        sums = family_sums(model, feats)
        if zone == "merge":
            fam = max((f for f in sums if f != "MISSING"), key=lambda f: sums[f], default="NONE")
            if sums.get(fam, 0.0) <= 0.0:
                fam = "NONE"
        else:
            fam = min((f for f in sums if f != "MISSING"), key=lambda f: sums[f], default="NONE")
            if sums.get(fam, 0.0) >= 0.0:
                fam = "NONE"
        return "model<" + zone + ">", fam, reason
    return "other", "NONE", reason


def edge_rank(row):
    return (0 if row.get("certificate") else 1, -float(row.get("score") or 0.0), row["lo"], row["hi"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("cohort")
    ap.add_argument("out_dir")
    ap.add_argument("--settings", default="w31")
    ap.add_argument("--model", default="w6_gold")
    args = ap.parse_args()
    run_dir, out = Path(args.run_dir), Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    settings = Settings.from_json(Path(WT) / f"autodedup/settings/{args.settings}.json")
    model = LogisticModel.from_json((Path(WT) / f"autodedup/models/{args.model}.json").read_text())
    ds = load(args.cohort)
    listings = ds.listings
    clusters = json.loads((run_dir / "clusters.json").read_text())
    conflict_ids = {int(m) for c in clusters.get("conflicts", []) for m in c.get("members", [])}
    conflict_feats: dict[tuple[int, int], dict] = {}
    member_of: dict[int, str] = {}
    for key, members in clusters["clusters"].items():
        if len(members) >= 2:
            for m in members:
                member_of[int(m)] = key

    pair_cells = collections.Counter()          # (zone, rung, family) -> pairs
    merge_detail = collections.Counter()        # (rung, family, detail) -> merge pairs
    nonmerge_detail = collections.Counter()     # (rung, family, detail) for gate/refusal
    reason_check = collections.Counter()
    mass = collections.defaultdict(lambda: collections.defaultdict(float))  # rung -> fam -> sum of positive share
    mass_n = collections.Counter()
    pivotal = collections.defaultdict(collections.Counter)  # rung -> fam -> n pivotal
    best_by_advert: dict[int, dict] = {}
    incident_merge: dict[int, list] = collections.defaultdict(list)
    unmapped = collections.Counter()
    n_pairs = 0
    with gzip.open(run_dir / "pairs.jsonl.gz", "rt") as fh:
        for line in fh:
            row = json.loads(line)
            n_pairs += 1
            reason_check[row.get("reason") or ""] += 1
            feats = feats_of(row.get("feats") or row.get("features"))
            if row["lo"] in conflict_ids and row["hi"] in conflict_ids:
                conflict_feats[(row["lo"], row["hi"])] = feats
            rung, fam, detail = classify(row, feats, model, settings, listings)
            if fam.startswith("UNMAPPED"):
                unmapped[fam] += 1
            zone = row["zone"] or "veto"
            pair_cells[(zone, rung, fam)] += 1
            cell = {"lo": row["lo"], "hi": row["hi"], "zone": zone, "rung": rung, "family": fam,
                    "detail": detail, "score": float(row.get("score") or 0.0),
                    "certificate": row.get("certificate")}
            if zone == "merge":
                merge_detail[(rung, fam, detail if not rung.startswith("model") else "")] += 1
                sums = family_sums(model, feats)
                pos = {f: v for f, v in sums.items() if v > 0}
                total = sum(pos.values()) or 1.0
                for f, v in pos.items():
                    mass[rung][f] += v / total
                mass_n[rung] += 1
                logit = model.score(feats)
                cut = stratum_t_hi(feats, None, settings) or 0.9788
                for f, v in pos.items():
                    if calibrated(model, logit - v) < cut:
                        pivotal[rung][f] += 1
                incident_merge[row["lo"]].append(cell)
                incident_merge[row["hi"]].append(cell)
            elif rung.startswith("d43_") or rung.startswith("hold"):
                nonmerge_detail[(rung, fam, detail)] += 1
            for side in (row["lo"], row["hi"]):
                prev = best_by_advert.get(side)
                order = {"merge": 0, "band": 1, "reject": 2, "veto": 3}
                key = (order.get(zone, 4), -cell["score"])
                if prev is None or key < prev["_key"]:
                    best_by_advert[side] = {**cell, "_key": key}

    # conflicts: refused joins, mapped to the refusing fact's family where the edge itself states it
    conflict_of: dict[int, dict] = {}
    conflict_cells = collections.Counter()
    relation = ClusterRelation(listings, conflict_feats, settings)
    for c in clusters.get("conflicts", []):
        inv = c.get("invariant")
        if inv == "d43_distinguishable":
            ids = [int(m) for m in c.get("members", [])]
            vp = relation.violating_pair(ids)
            strict = ""
            if vp is None:
                vp = relation.strict().violating_pair(ids)
                strict = " (strict re-offer)"
            if vp is None:
                fam, det = "GROUP", "no_violating_pair_found"
            else:
                la, lb = listings[vp[0]], listings[vp[1]]
                facts = distinguishing_facts(la, lb, conflict_feats.get(vp), settings, CLUSTER)
                if facts:
                    fam, det = fact_family(facts[0].name), facts[0].name
                else:
                    fam, det = "PRICE", "cluster_price_conflict(E157)"
                det += strict
                det += " [on the edge]" if vp == (min(c["lo"], c["hi"]), max(c["lo"], c["hi"])) else " [other members]"
        elif inv == "must_not_link":
            fam, det = "OPERATOR", inv
        elif inv in ("area_spread", "category_type", "category_main", "size"):
            fam, det = ("ATTR" if inv != "size" else "NONE"), inv
        else:
            fam, det = "NONE", str(inv)
        conflict_cells[(inv, fam, det)] += 1
        for side in (c["lo"], c["hi"]):
            conflict_of.setdefault(side, {"rung": "cluster_refused<" + str(inv) + ">",
                                          "family": fam, "detail": det})

    advert_cells = collections.Counter()
    advert_detail = collections.Counter()
    for lid in listings:
        if lid in member_of:
            key = member_of[lid]
            members = set(clusters["clusters"][key])
            edges = [e for e in incident_merge.get(lid, []) if e["lo"] in members and e["hi"] in members]
            if edges:
                best = min(edges, key=edge_rank)
                advert_cells[("grouped", best["rung"], best["family"])] += 1
            else:
                advert_cells[("grouped", "must_link", "OPERATOR")] += 1
            continue
        if lid in conflict_of:
            c = conflict_of[lid]
            advert_cells[("alone", c["rung"], c["family"])] += 1
            advert_detail[("alone", c["rung"], c["family"], c["detail"])] += 1
            continue
        best = best_by_advert.get(lid)
        if best is None:
            advert_cells[("alone", "no_stored_pair", "NONE")] += 1
            continue
        if best["zone"] == "merge":
            # a merge edge whose far side sits in a group this advert is not in: refused by invariants
            advert_cells[("alone", "merge_edge_unclustered", best["family"])] += 1
            continue
        advert_cells[("alone", best["zone"] + ":" + best["rung"], best["family"])] += 1
        if best["rung"].startswith("d43_"):
            advert_detail[("alone", best["rung"], best["family"], best["detail"])] += 1

    result = {
        "run_dir": str(run_dir), "cohort": args.cohort, "settings": args.settings,
        "model": args.model, "n_adverts": len(listings), "n_grouped": len(member_of),
        "n_stored_pairs": n_pairs,
        "pair_cells": [[z, r, f, n] for (z, r, f), n in sorted(pair_cells.items())],
        "merge_detail": [[r, f, d, n] for (r, f, d), n in sorted(merge_detail.items())],
        "nonmerge_detail": [[r, f, d, n] for (r, f, d), n in sorted(nonmerge_detail.items())],
        "advert_cells": [[g, r, f, n] for (g, r, f), n in sorted(advert_cells.items())],
        "advert_detail": [[g, r, f, d, n] for (g, r, f, d), n in sorted(advert_detail.items())],
        "conflict_cells": [[i, f, d, n] for (i, f, d), n in sorted(conflict_cells.items())],
        "mass_share": {r: {f: v / mass_n[r] for f, v in fams.items()} for r, fams in mass.items()},
        "mass_n": dict(mass_n),
        "pivotal": {r: dict(c) for r, c in pivotal.items()},
        "reason_counts": dict(reason_check),
        "unmapped": dict(unmapped),
    }
    (out / "attribution.json").write_text(json.dumps(result, indent=1, sort_keys=True))
    write_md(result, out / "attribution.md")
    print("done", out)


def table(rows_by, cols, fmt=lambda v: f"{v:,}" if v else "-"):
    lines = ["| | " + " | ".join(cols) + " | total |", "|---|" + "---:|" * (len(cols) + 1)]
    for rname, vals in rows_by:
        tot = sum(vals.get(c, 0) for c in cols)
        lines.append(f"| {rname} | " + " | ".join(fmt(vals.get(c, 0)) for c in cols) + f" | {tot:,} |")
    colsum = {c: sum(v.get(c, 0) for _, v in rows_by) for c in cols}
    lines.append("| **total** | " + " | ".join(f"{colsum[c]:,}" for c in cols)
                 + f" | {sum(colsum.values()):,} |")
    return "\n".join(lines)


def write_md(r, path: Path) -> None:
    fams = [f for f in FAMILIES]
    used = set()
    for _, _, f, n in r["advert_cells"]:
        used.add(f)
    for _, _, f, n in r["pair_cells"]:
        used.add(f)
    fams = [f for f in FAMILIES if f in used] + sorted(f for f in used if f not in FAMILIES)
    out = [f"# Attribution: {r['run_dir']}", "",
           f"settings {r['settings']}, model {r['model']}; adverts {r['n_adverts']:,}, "
           f"grouped {r['n_grouped']:,}; stored pairs {r['n_stored_pairs']:,}", ""]
    # T1 merge pairs
    merge_rows = collections.defaultdict(dict)
    for z, rung, f, n in r["pair_cells"]:
        if z == "merge":
            merge_rows[rung][f] = merge_rows[rung].get(f, 0) + n
    out += ["## T1 merge decisions (pair grain): rung x carrying family", "",
            table(sorted(merge_rows.items()), fams), ""]
    # T2 advert grain
    adv = collections.defaultdict(dict)
    for g, rung, f, n in r["advert_cells"]:
        adv[f"{g} / {rung}"][f] = adv[f"{g} / {rung}"].get(f, 0) + n
    out += ["## T2 every advert (advert grain): outcome / rung x family", "",
            table(sorted(adv.items()), fams), ""]
    # T3 non-merge stored decisions
    nm = collections.defaultdict(dict)
    for z, rung, f, n in r["pair_cells"]:
        if z != "merge":
            nm[f"{z} / {rung}"][f] = nm[f"{z} / {rung}"].get(f, 0) + n
    out += ["## T3 stored non-merge decisions (pair grain)", "", table(sorted(nm.items()), fams), ""]
    # T4 model mass
    mf = [f for f in ("ATTR", "PRICE", "TXT", "BRK", "LOC", "IMG", "TIME", "MISSING")]
    out += ["## T4 positive log-odds share by family over merge edges (w6_gold contributions), and "
            "pivotal count (removing that family alone drops the pair below the 0.9788 cut)", "",
            "| rung | n | " + " | ".join(mf) + " |", "|---|---:|" + "---:|" * len(mf)]
    for rung in sorted(r["mass_share"]):
        share = r["mass_share"][rung]
        piv = r["pivotal"].get(rung, {})
        out.append(f"| {rung} | {r['mass_n'][rung]:,} | " + " | ".join(
            f"{share.get(f, 0.0) * 100:.1f}% ({piv.get(f, 0):,})" for f in mf) + " |")
    out += ["", "## T5 gate demotions, promotion refusals and holds by reader (pair grain)", "",
            "| rung | family | reader / detail | pairs |", "|---|---|---|---:|"]
    for rung, f, d, n in sorted(r["nonmerge_detail"], key=lambda x: (x[0], -x[3])):
        out.append(f"| {rung} | {f} | {d} | {n:,} |")
    out += ["", "## T6 promotions by carrying corroboration (merge pairs)", "",
            "| rung | family | detail | pairs |", "|---|---|---|---:|"]
    for rung, f, d, n in sorted(r["merge_detail"], key=lambda x: (x[0], -x[3])):
        if rung.startswith("d43_promote") or rung.startswith("certificate"):
            out.append(f"| {rung} | {f} | {d} | {n:,} |")
    out += ["", "## T7 group refusals (conflicts) by invariant and refusing fact", "",
            "| invariant | family | fact | conflicts |", "|---|---|---|---:|"]
    for inv, f, d, n in sorted(r["conflict_cells"], key=lambda x: -x[3]):
        out.append(f"| {inv} | {f} | {d} | {n:,} |")
    if r["unmapped"]:
        out += ["", "UNMAPPED: " + json.dumps(r["unmapped"])]
    path.write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
