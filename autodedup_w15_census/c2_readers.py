"""The D43 fact readers, the D50 demonstration limbs and the promotion warrant, counted and
knocked out one at a time over one cohort, each against this cohort's FULL.

    python3 c2_readers.py <cohort> [stored_pairs.jsonl.gz]

Census: every `distinguishing_facts` call the FULL pass makes is logged with its phase (decide /
cluster) and mode (gate / promote / cluster), so each reader's firings are counted per reading.
Knockout: the reader's fact is filtered out of every call; only the pairs it fired on in the
decide phase are re-decided; the clustering is re-run whole (its relation re-reads the reader).
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import c2lib  # noqa: E402
from autodedup import demonstrate as demo_mod  # noqa: E402
from autodedup.settings import Settings  # noqa: E402

OUT = Path("/home/hejtm/autodedup-artifacts/w15/census/c2")

READERS = [
    "category_type", "category_main", "area", "stated_area", "disposition", "floor",
    "total_floors", "plot_area", "price", "obec", "street", "orientation", "unit_designator",
    "parcel", "accessory", "extent", "two_unit", "printed_area", "offer_area", "prose_floor",
    "subject_floor", "storey_word", "unit_count", "unit_code", "street_prose", "obec_prose",
    "agency_code", "charge", "plot_prose", "labelled_unit", "headline_area", "offered_storey",
    "accessory_area", "cellar_area", "body_obec", "plot_prose_exact", "priced_row",
    "neighbour_plot", "part_whole", "rental_colive", "english_unit_code", "slug_unit",
    "agency_code_plus", "offered_use", "commercial_subtype", "plot_attribute", "product_class",
    "space_number", "part_addition", "printed_house_number", "stored_house_number",
    "printed_designator", "plan_space", "extent_variant", "lot_label", "extent_package",
    "agency_code_colive", "outdoor_accessory", "accessory_price", "position_designator",
    "named_villa", "body_align", "floorplan", "interior",
]

SETTINGS_ARMS = {
    # the whole layers
    "d43_gate_off": ({"d43_gate": False}, "pre_merge"),
    "d43_promote_off": ({"d43_promote": False, "d43_promote_photo_alternative": False,
                         "d43_promote_unit_evidence": False}, "pre_band"),
    "d43_cluster_invariant_off": ({"d43_cluster_invariant": False}, None),
    "cluster_image_facts_off": ({"d43_cluster_image_facts": False}, None),
    "cluster_price_limb_off": ({"demonstrate_cluster_price": False}, None),
    # D50 limbs with a dial
    "demo_disposition_off": ({"demonstrate_require_disposition": False}, "pre_band"),
    "demo_obec_off": ({"demonstrate_require_obec": False}, "pre_band"),
    "demo_onesided_off": ({"demonstrate_onesided": False}, "pre_band"),
    "demo_recover_missing_off": ({"demonstrate_recover_missing": False}, "pre_band"),
    "demo_B_off": ({"corroboration": "off"}, "pre_band"),
    "demo_twin_off": ({"demonstrate_identical_twin": False}, "pre_band"),
    # the promotion warrant (E131/E191): does it add anything the demonstration does not?
    "warrant_predicate_only": ({"d43_promote_min_agreeing": 0}, "pre_band"),
    "warrant_photo_alt_off": ({"d43_promote_photo_alternative": False}, "pre_band"),
    "warrant_unit_evidence_off": ({"d43_promote_unit_evidence": False}, "pre_band"),
}


def settings_with(eng, patch):
    return Settings.from_dict({**eng.settings.to_dict(), **patch})


def arm_result(eng, post, settings, ops, extra):
    run = eng.cluster(post, settings)
    res = c2lib.compare(eng, run, ops)
    res.update(extra)
    res["decisions_changed"] = sum(1 for a, b in zip(eng.base.decisions, post)
                                   if a.zone != b.zone or a.reason != b.reason)
    res["flipped_pairs"] = [[a.lo, a.hi, a.zone, a.reason, b.zone, b.reason]
                            for a, b in zip(eng.base.decisions, post) if a.zone != b.zone][:300]
    return res


def main() -> None:
    cohort = sys.argv[1]
    stored = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    out_dir = OUT / f"readers_{cohort}"
    out_dir.mkdir(parents=True, exist_ok=True)
    eng = c2lib.Engine.build(cohort, OUT / "cache")
    c2lib.LOG = []
    clock = time.perf_counter()
    eng.baseline()
    log = c2lib.LOG
    c2lib.LOG = None
    print(f"[{cohort}] FULL in {time.perf_counter()-clock:.0f}s, {len(log)} fact calls", flush=True)
    ops = c2lib.load_operator(eng)
    index = {k: i for i, k in enumerate(eng.keys)}
    import gzip
    with gzip.open(out_dir / "full_decisions.jsonl.gz", "wt") as handle:
        for p0, d in zip(eng.pre, eng.base.decisions):
            handle.write(json.dumps([d.lo, d.hi, d.zone, d.reason, d.certificate, d.score,
                                     sorted(d.families), p0.zone, p0.reason]) + "\n")
    (out_dir / "full_clusters.json").write_text(json.dumps(
        {str(k): v for k, v in eng.base.clusters.items() if len(v) > 1}))

    base = {}
    if stored is not None:
        base["verify"] = c2lib.verify_against(eng, stored)
        print(json.dumps(base["verify"])[:600], flush=True)

    # ---- census
    seen: dict[tuple[str, str], dict[tuple[int, int], tuple[str, ...]]] = defaultdict(dict)
    for phase, mode, key, names in log:
        seen[(phase, mode)][key] = names
    census = {r: Counter() for r in READERS}
    fired_decide: dict[str, set[int]] = defaultdict(set)
    for (phase, mode), rows in seen.items():
        tag = f"{phase}_{mode}"
        for key, names in rows.items():
            for pos, name in enumerate(names):
                census.setdefault(name, Counter())
                census[name][f"{tag}_fires"] += 1
                if pos == 0:
                    census[name][f"{tag}_first"] += 1
                if len(names) == 1:
                    census[name][f"{tag}_sole"] += 1
                if phase == "decide" and key in index:
                    fired_decide[name].add(index[key])
    evaluated = {f"{p}_{m}": len(rows) for (p, m), rows in seen.items()}
    evaluated_nonempty = {f"{p}_{m}": sum(1 for v in rows.values() if v)
                          for (p, m), rows in seen.items()}
    reasons = Counter()
    for d in eng.base.decisions:
        if ":d43_gate:" in d.reason:
            reasons["gate:" + d.reason.split(":d43_gate:")[1]] += 1
        if ":d43_demonstrate:" in d.reason:
            reasons["demonstrate:" + d.reason.split(":d43_demonstrate:")[1]] += 1
        if d.reason.startswith("d43_promote:"):
            reasons["promote:" + d.reason.split(":", 1)[1]] += 1
    pre_zone = Counter(p.zone for p in eng.pre)
    (out_dir / "census.json").write_text(json.dumps({
        "evaluated_pairs": evaluated, "evaluated_nonempty": evaluated_nonempty,
        "pre_d43_zones": pre_zone, "decision_reasons": reasons,
        "readers": {r: dict(census.get(r, {})) for r in sorted(census)},
        "never_fired": [r for r in READERS if not census.get(r)],
        **base,
    }, indent=1, sort_keys=True))
    print(f"[{cohort}] census written; never fired: "
          f"{[r for r in READERS if not census.get(r)]}", flush=True)

    # ---- reader knockouts
    for reader in READERS:
        if not census.get(reader):
            continue
        path = out_dir / f"ko_{reader}.json"
        if path.is_file():
            continue
        clock = time.perf_counter()
        c2lib.DISABLED.clear()
        c2lib.DISABLED.add(reader)
        try:
            only = sorted(fired_decide.get(reader, ()))
            _, post = eng.decide_all(eng.model, eng.settings, only=only,
                                     base=eng.base.decisions)
            res = arm_result(eng, post, eng.settings, ops,
                             {"reader": reader, "redecided": len(only)})
        finally:
            c2lib.DISABLED.clear()
        res["seconds"] = time.perf_counter() - clock
        path.write_text(json.dumps(res, indent=1))
        print(f"[{cohort}] ko {reader}: merge {res['merge_base']}->{res['merge']} "
              f"groups {res['groups_base']}->{res['groups']} diff {res['groups_differing']} "
              f"op_merge {res['op_merge_pairs_together_base']}->{res['op_merge_pairs_together']} "
              f"op_diff {res['op_diff_together_base']}->{res['op_diff_together']} "
              f"{res['seconds']:.0f}s", flush=True)

    # ---- layer / limb / warrant knockouts
    pre_merge = [i for i, p in enumerate(eng.pre) if p.zone == "merge"]
    pre_band = [i for i, p in enumerate(eng.pre) if p.zone == "band"]
    arms = dict(SETTINGS_ARMS)
    for name, (patch, scope) in arms.items():
        path = out_dir / f"{name}.json"
        if path.is_file():
            continue
        clock = time.perf_counter()
        try:
            settings = settings_with(eng, patch)
        except ValueError as error:
            path.write_text(json.dumps({"patch": patch, "error": str(error)}))
            print(f"[{cohort}] {name}: settings refused: {error}", flush=True)
            continue
        only = {"pre_merge": pre_merge, "pre_band": pre_band, None: []}[scope]
        _, post = eng.decide_all(eng.model, settings, only=only, base=eng.base.decisions)
        res = arm_result(eng, post, settings, ops, {"patch": patch, "redecided": len(only)})
        res["seconds"] = time.perf_counter() - clock
        path.write_text(json.dumps(res, indent=1))
        print(f"[{cohort}] {name}: merge {res['merge_base']}->{res['merge']} "
              f"groups {res['groups_base']}->{res['groups']} diff {res['groups_differing']} "
              f"op_merge {res['op_merge_pairs_together_base']}->{res['op_merge_pairs_together']} "
              f"op_diff {res['op_diff_together_base']}->{res['op_diff_together']} "
              f"{res['seconds']:.0f}s", flush=True)

    # D50 limbs without a dial: area and price, patched in the decide phase only; and D50's
    # pair-grain refusal as a whole (the cluster price limb stays).
    from autodedup import decide as decide_mod
    for name, module, attr in (("demo_area_off", demo_mod, "area_demonstrated"),
                               ("demo_price_off", demo_mod, "price_demonstrated"),
                               ("d50_pair_off", decide_mod, "demonstration_refusal")):
        path = out_dir / f"{name}.json"
        if path.is_file():
            continue
        clock = time.perf_counter()
        original = getattr(module, attr)
        setattr(module, attr, (lambda *a, **k: None) if attr == "demonstration_refusal"
                else (lambda *a, **k: True))
        try:
            _, post = eng.decide_all(eng.model, eng.settings, only=pre_band,
                                     base=eng.base.decisions)
        finally:
            setattr(module, attr, original)
        res = arm_result(eng, post, eng.settings, ops, {"patched": attr, "redecided": len(pre_band)})
        res["seconds"] = time.perf_counter() - clock
        path.write_text(json.dumps(res, indent=1))
        print(f"[{cohort}] {name}: merge {res['merge_base']}->{res['merge']} "
              f"groups {res['groups_base']}->{res['groups']} diff {res['groups_differing']} "
              f"op_merge {res['op_merge_pairs_together_base']}->{res['op_merge_pairs_together']} "
              f"op_diff {res['op_diff_together_base']}->{res['op_diff_together']} "
              f"{res['seconds']:.0f}s", flush=True)
    print(f"[{cohort}] readers done", flush=True)


if __name__ == "__main__":
    main()
