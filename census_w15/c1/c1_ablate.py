"""C1 ablation runner: one arm = one rule switched off, replayed against the w31 baseline.

    python3 c1_ablate.py <cohort-name> [--arms a,b,c | --group G] [--list]

cohort-name: trial | c17 | c18. Appends one JSON line per arm to
../runs/<cohort>_arms.jsonl (resumable: an arm already present is skipped).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import c1_engine as E  # noqa: E402
from autodedup.settings import Settings  # noqa: E402

ROOT = Path("/home/hejtm/autodedup-artifacts/w15/census/c1")
S15 = Path("/home/hejtm/autodedup-artifacts/w14/s15")
COHORTS = {
    "trial": (str(S15 / "score_g15/autodedup-score-36244048665/artifact/cohort.jsonl.gz"),
              None),
    "trialr": (str(S15 / "score_g15/autodedup-score-36244048665/artifact/cohort.jsonl.gz"),
               str(S15 / "read17_dryrun_g13/ref")),
    "c17": (str(S15 / "cohort17_export_36221961445/autodedup-export-36221961445/cohort.jsonl.gz"),
            str(S15 / "cohort17_read/ref")),
    "c18": (str(S15 / "cohort18_export_36237638871/autodedup-export-36237638871/cohort.jsonl.gz"),
            str(S15 / "cohort18_read/ref")),
}
# every dial `_decide_layers` (or blocking) reads: an arm touching one cannot reuse the layers
LAYER_KEYS = {
    "unit_designator_veto", "max_attr_contradictions", "certificate_ka_enabled",
    "certificate_kr_enabled", "certificate_b_min_images", "certificate_b_min_matched_images",
    "certificate_b_min_gap_days", "t_hi", "t_hi_by_stratum", "t_lo", "min_evidence_families",
    "developer_signature_guard", "developer_colive_guard", "unit_evidence_required",
    "live_window_from_sighting", "area_reject_pct", "area_band_pct",
}

BASE_DICT = None


def S(**over) -> Settings:
    return Settings.from_dict({**BASE_DICT, **over})


def coupled(over: dict) -> tuple[Settings, dict]:
    """The arm's override plus every dial the validator says depends on it ("X needs Y"),
    switched off too - a rule cannot be ablated without its dependants."""
    import re
    over = dict(over)
    extra: dict = {}
    for _ in range(20):
        try:
            return Settings.from_dict({**BASE_DICT, **over}), extra
        except ValueError as exc:
            m = re.match(r"(\w+) needs ", str(exc))
            if not m:
                raise
            name = m.group(1)
            cur = BASE_DICT.get(name)
            dflt = getattr(Settings(), name, None)
            off = False if isinstance(cur, bool) else (
                (dflt if dflt != cur else "off") if isinstance(cur, str) else None)
            if off is None or over.get(name) == off:
                raise
            over[name] = off
            extra[name] = off
    raise ValueError("coupling did not converge")


def arms(base: Settings) -> list[dict]:
    """(name, layer, override, patch, reblock, extra)"""
    A: list[dict] = []

    def add(name, layer, over=None, patch=None, reblock=False, **extra):
        A.append({"name": name, "layer": layer, "over": over or {}, "patch": patch,
                  "reblock": reblock, **extra})

    # L0 blocking walls (guards.pair_veto, also re-read at decide 7.1)
    for wall in ("category_type", "category_main", "area", "disposition", "floor"):
        add(f"wall:{wall}", "blocking-veto", patch=("drop_wall", wall), reblock=True)
    add("wall:ALL", "blocking-veto", patch=("no_walls",), reblock=True)
    # L1 rule floor
    add("E61:unit_designator_veto", "decide-veto", {"unit_designator_veto": False})
    add("E61:cluster_machine_veto_only", "cluster", machine_vetoes=False)
    add("auto_reject:attr_contradictions", "decide-reject", patch=("auto_reject", "numeral_conflict"))
    add("auto_reject:numeral_conflict", "decide-reject", patch=("auto_reject", "attr_contradictions"))
    add("auto_reject:BOTH", "decide-reject", patch=("auto_reject", None))
    # L2 certificates
    add("cert:K-R", "decide-cert", {"certificate_kr_enabled": False})
    add("cert:K-B", "decide-cert", patch=("cert", "K-B"))
    add("cert:K-C", "decide-cert", patch=("cert", "K-C"))
    add("cert:K-B_gap_E84", "decide-cert", {"certificate_b_min_gap_days": 0.0,
                                             "live_window_from_sighting": False},
        note="E84 cannot be dropped alone (validator couples it to the honest clock)")
    # L3 gates / cut / zones
    add("E11:evidence_gate", "decide-gate", {"min_evidence_families": 0})
    add("E48:strata_table->t_hi", "decide-cut", {"t_hi_by_stratum": {}, "t_hi": 0.9788})
    add("cut:model_merge_authority", "decide-cut",
        {"t_hi_by_stratum": {**base.t_hi_by_stratum, "model|cross": None, "model|same": None}})
    add("zone:t_lo_floor->store_floor", "decide-zone", {"t_lo": 0.02})
    add("zone:no_band(t_lo->t_hi)", "decide-zone", {"t_lo": 0.9788})
    # L4 E63
    add("E63:context_rule", "decide-E63", {"context_rule_enabled": False})
    # L5 D43
    add("D43:gate", "decide-D43", {"d43_gate": False})
    add("D43:promote", "decide-D43", {"d43_promote": False})
    add("D43:warrant->predicate", "decide-D43", {"d43_promote_min_agreeing": 0})
    add("D43:warrant_photo_alt", "decide-D43", {"d43_promote_photo_alternative": False})
    add("D43:warrant_unit_evidence", "decide-D43", {"d43_promote_unit_evidence": False})
    add("D43:warrant_unit_body_sequential", "decide-D43", {"d43_promote_unit_body_sequential": False})
    # L6 D50
    add("D50:demonstration", "decide-D50", {"demonstrate_identity": False})
    add("D50:A:area", "decide-D50", patch=("demo_limb", "area"))
    add("D50:A:disposition", "decide-D50", {"demonstrate_require_disposition": False})
    add("D50:A:price", "decide-D50", patch=("demo_limb", "price"))
    add("D50:A:obec", "decide-D50", {"demonstrate_require_obec": False})
    add("D50:A:onesided", "decide-D50", {"demonstrate_onesided": False})
    add("D50:B:corroboration", "decide-D50", {"corroboration": "off"})
    add("D50:A:recover_missing", "decide-D50", {"demonstrate_recover_missing": False})
    add("D50:B:identical_twin", "decide-D50", {"demonstrate_identical_twin": False})
    # L7 cluster
    add("C:d43_cluster_invariant", "cluster", {"d43_cluster_invariant": False})
    add("C:E157_cluster_price", "cluster", {"demonstrate_cluster_price": False})
    add("C:cluster_image_facts", "cluster", {"d43_cluster_image_facts": False})
    add("C:area_spread", "cluster", {"cluster_area_spread": 1.0})
    add("C:max_cluster_size", "cluster", {"max_cluster_size": 10 ** 9})
    add("C:category_type_inv", "cluster", patch=("invariant", "category_type"))
    add("C:compat_class_inv", "cluster", patch=("invariant", "compat_class"))
    add("C:repartition(E137)->unionfind", "cluster", {"repartition": False})
    add("C:repartition->unionfind_no_bridges", "cluster", {"repartition": False, "bridge_apply": False})
    add("C:keep_factless(E156)", "cluster", {"repartition_keep_factless": False})
    add("C:rejoin_cells(E193)", "cluster", {"repartition_rejoin_cells": False})
    add("C:shed_blockers(E253)", "cluster", {"repartition_shed_blockers": False})
    add("C:outer_rounds(E253)->1", "cluster", {"repartition_outer_rounds": 1})
    add("C:shed_factless_guard(E262)", "cluster", {"repartition_shed_factless_guard": "off"})
    add("C:reconcile_factless_first(E263)", "cluster", {"repartition_reconcile_factless_first": False})
    add("C:repartition_ALL_repairs", "cluster", {"repartition_keep_factless": False,
                                                  "repartition_rejoin_cells": False,
                                                  "repartition_shed_blockers": False,
                                                  "repartition_outer_rounds": 1})
    zs = ROOT / "trial_zero_sets.json"
    if zs.exists():
        z = json.loads(zs.read_text())
        over = {}
        for name in z["zero_dials"]:
            cur = BASE_DICT.get(name)
            if isinstance(cur, bool):
                over[name] = not cur
            elif isinstance(cur, str):
                dflt = getattr(Settings(), name)
                over[name] = dflt if dflt != cur else "off"
        add("bundle:trial_zero_dials", "bundle", over)
        add("bundle:trial_zero_facts", "bundle", patch=("drop_facts_set", tuple(z["zero_facts"])))
    add("bundle:c1_code_bundle_settings", "bundle",
        {"t_hi": 0.9788, "t_hi_by_stratum": {}, "context_rule_enabled": False, "d43_gate": False,
         "unit_designator_veto": False, "repartition_keep_factless": False,
         "repartition_rejoin_cells": False, "repartition_shed_blockers": False,
         "repartition_outer_rounds": 1},
        patch=("invariant_set", ("size", "category_type", "compat_class", "area_spread")))
    return A


def fact_arms(names: list[str]) -> list[dict]:
    return [{"name": f"fact:{n}", "layer": "D43-reader", "over": {}, "patch": ("drop_facts", n),
             "reblock": False} for n in names]


EXPLICIT_DIALS = set()


def dial_arms(base: Settings, done: set[str]) -> list[dict]:
    """Every remaining ON boolean / mode dial of the D43/D50/floor/repartition families."""
    out = []
    default = Settings()
    for f in dataclasses.fields(Settings):
        name = f.name
        if name in EXPLICIT_DIALS:
            continue
        if not (name.startswith(("d43_", "demonstrate_", "floor_", "repartition_",
                                 "corroboration", "development_context"))):
            continue
        value = getattr(base, name)
        if isinstance(value, bool):
            if value:
                out.append({"name": f"dial:{name}", "layer": "dial", "over": {name: False},
                            "patch": None, "reblock": False})
        elif isinstance(value, str):
            dv = getattr(default, name)
            off = "off" if value != "off" else None
            target = dv if dv != value else off
            if target is not None and target != value:
                out.append({"name": f"dial:{name}", "layer": "dial", "over": {name: target},
                            "patch": None, "reblock": False})
        elif name == "floor_camps" and value:
            out.append({"name": "dial:floor_camps", "layer": "dial", "over": {name: {}},
                        "patch": None, "reblock": False})
    return [a for a in out if a["name"] not in done]


def make_patch(spec):
    if spec is None:
        return None
    kind = spec[0]
    if kind == "drop_wall":
        return E.patch_drop_wall(spec[1])
    if kind == "no_walls":
        return E.patch_no_walls()
    if kind == "auto_reject":
        return E.patch_auto_reject(spec[1])
    if kind == "cert":
        return E.patch_cert(spec[1])
    if kind == "drop_facts":
        return E.patch_drop_facts({spec[1]})
    if kind == "drop_facts_set":
        return E.patch_drop_facts(set(spec[1]))
    if kind == "invariant_set":
        return E.patch_invariants_off(set(spec[1]))
    if kind == "demo_limb":
        return E.patch_demo_drop_limb(spec[1])
    if kind == "invariant":
        return E.patch_invariant_off(spec[1])
    raise ValueError(spec)


def main() -> None:
    global BASE_DICT
    ap = argparse.ArgumentParser()
    ap.add_argument("cohort")
    ap.add_argument("--arms", default=None)
    ap.add_argument("--group", default="all", choices=["all", "rules", "facts", "dials", "none"])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--adhoc", default=None,
                    help="a JSON file: [{name, over, patch?}] run as extra arms")
    args = ap.parse_args()
    cohort, ref = COHORTS[args.cohort]
    wt = E.WT
    base = Settings.from_json(Path(wt) / "autodedup/settings/w31.json")
    BASE_DICT = base.to_dict()
    for a in arms(base):
        for k in a["over"]:
            EXPLICIT_DIALS.add(k)
    out_path = ROOT / "runs" / f"{args.cohort}_arms.jsonl"
    done = set()
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            if line.strip():
                done.add(json.loads(line)["name"])
    fact_names = sorted(json.loads((ROOT / "fact_names.json").read_text()))
    plan = []
    if args.group in ("all", "rules"):
        plan += arms(base)
    if args.group in ("all", "facts"):
        plan += fact_arms(fact_names)
    if args.group in ("all", "dials"):
        plan += dial_arms(base, done)
    if args.arms:
        wanted = set(args.arms.split(","))
        every = arms(base) + fact_arms(fact_names) + dial_arms(base, set())
        plan = [a for a in every if a["name"] in wanted]
    if args.adhoc:
        for row in json.loads(Path(args.adhoc).read_text()):
            patch = tuple(row["patch"]) if row.get("patch") else None
            if patch and patch[0] == "drop_facts_set":
                patch = (patch[0], tuple(patch[1]))
            plan.append({"name": row["name"], "layer": row.get("layer", "adhoc"),
                         "over": row.get("over", {}), "patch": patch, "reblock": False})
    plan = [a for a in plan if a["name"] not in done]
    if args.list:
        for a in plan:
            print(a["name"], a["layer"], a["over"], a["patch"])
        print(len(plan), "arms")
        return
    eng = E.Engine(cohort, str(Path(wt) / "autodedup/settings/w31.json"),
                   str(Path(wt) / "autodedup/models/w6_gold.json"),
                   str(ROOT / f"cache_{args.cohort}.pkl"), ref=ref)
    print(f"setup {eng.setup_s:.0f}s pairs {len(eng.base_pairs)} labels "
          f"same {sum(1 for v in eng.labels['verdict'].values() if v == 'same')} "
          f"diff {sum(1 for v in eng.labels['verdict'].values() if v == 'different')} "
          f"browse {len(eng.labels['browse_merge'])}", flush=True)
    base_pkl = ROOT / "runs" / f"{args.cohort}_base.pkl"
    if base_pkl.exists():
        with open(base_pkl, "rb") as fh:
            base_sum = pickle.load(fh)
    else:
        res = eng.run(base, log_facts=True)
        base_sum = E.summarize(eng, res)
        with open(base_pkl, "wb") as fh:
            pickle.dump(base_sum, fh)
        facts = res["facts"]
        with gzip_open(ROOT / "runs" / f"{args.cohort}_base_facts.jsonl.gz") as fh:
            for (k, mode), names in sorted(facts.items()):
                fh.write(json.dumps({"lo": k[0], "hi": k[1], "mode": mode,
                                     "facts": list(names)}) + "\n")
        with open(ROOT / "runs" / f"{args.cohort}_base_price_limb.json", "w") as fh:
            json.dump(sorted(res["price_log"] or []), fh)
        decisions = res["decisions"]
        with gzip_open(ROOT / "runs" / f"{args.cohort}_base_decisions.jsonl.gz") as fh:
            for d in decisions:
                row = d.to_json()
                row.pop("families", None)
                fh.write(json.dumps(row) + "\n")
        with open(ROOT / "runs" / f"{args.cohort}_base_clusters.json", "w") as fh:
            json.dump({"clusters": {str(k): v for k, v in res["clusters"].clusters.items()},
                       "conflicts": res["clusters"].conflicts,
                       "stats": res["clusters"].stats}, fh)
        print("base", json.dumps(base_sum[0], default=str)[:600], flush=True)
    with open(out_path, "a") as sink:
        for a in plan:
            clock = time.perf_counter()
            try:
                cfg, extra = coupled(a["over"]) if a["over"] else (base, {})
                if extra:
                    a = {**a, "over": {**a["over"], **extra}, "coupled_off": extra}
            except Exception as exc:  # a coupled dial: record, do not guess
                sink.write(json.dumps({"name": a["name"], "layer": a["layer"],
                                       "over": a["over"], "invalid": str(exc)[:300]}) + "\n")
                sink.flush()
                print(a["name"], "INVALID", str(exc)[:120], flush=True)
                continue
            post_only = (not a["reblock"] and not (set(a["over"]) & LAYER_KEYS)
                         and (a["patch"] is None or a["patch"][0] in
                              ("drop_facts", "drop_facts_set", "demo_limb", "invariant",
                               "invariant_set")))
            res = eng.run(cfg, patch=make_patch(a["patch"]), reblock=a["reblock"],
                          post_only=post_only, machine_vetoes=a.get("machine_vetoes", True))
            arm_sum = E.summarize(eng, res)
            row = {"name": a["name"], "layer": a["layer"], "over": a["over"],
                   "patch": a["patch"], "post_only": post_only,
                   "summary": arm_sum[0], "reasons": dict(arm_sum[1]),
                   "delta": E.diff_against(eng, base_sum, arm_sum),
                   "wall_s": time.perf_counter() - clock, "note": a.get("note"),
                   "coupled_off": a.get("coupled_off")}
            sink.write(json.dumps(row, default=str) + "\n")
            sink.flush()
            pdir = ROOT / "runs" / f"{args.cohort}_arm_pairs"
            pdir.mkdir(exist_ok=True)
            (pdir / (a["name"].replace("/", "_").replace(">", "_") + ".json")).write_text(json.dumps({
                "gained": sorted(arm_sum[3] - base_sum[3]), "lost": sorted(base_sum[3] - arm_sum[3]),
                "merge_gained": sorted(arm_sum[2] - base_sum[2]),
                "merge_lost": sorted(base_sum[2] - arm_sum[2])}))
            d = row["delta"]
            print(f"{a['name']:<48} merge {d['merge_pairs_delta']:+6d} groups "
                  f"{arm_sum[0]['groups'] - base_sum[0]['groups']:+5d} copairs +{d['copairs_gained']}"
                  f"/-{d['copairs_lost']} gl {d['gained_labels']} ll {d['lost_labels']} "
                  f"{row['wall_s']:.0f}s", flush=True)


def gzip_open(path):
    import gzip
    return gzip.open(path, "wt", encoding="utf-8")


if __name__ == "__main__":
    main()
