"""Compare two harness run dirs: zones, reasons, merge set, groups, co-pairs, and the
operator-label / reference yardsticks. python3 c1_compare_runs.py <base_dir> <arm_dir> [ref_dir]"""
import gzip, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import c1_engine as E

def load(run):
    if str(run).startswith("replay:"):
        cohort = str(run).split(":", 1)[1]
        root = Path("/home/hejtm/autodedup-artifacts/w15/census/c1/runs")
        merge = set(); zones = {}
        for l in gzip.open(root / f"{cohort}_base_decisions.jsonl.gz", "rt"):
            d = json.loads(l)
            if d["zone"] == "merge":
                merge.add((d["lo"], d["hi"]))
            if d["zone"] in ("merge", "band", "veto") or d["score"] >= 0.02:
                zones[d["zone"]] = zones.get(d["zone"], 0) + 1
        c = json.loads((root / f"{cohort}_base_clusters.json").read_text())
        run = None
    else:
        run = Path(run)
    if run is not None:
        merge = set(); zones = {}
        for l in gzip.open(run / "pairs.jsonl.gz", "rt"):
            d = json.loads(l)
            zones[d["zone"]] = zones.get(d["zone"], 0) + 1
            if d["zone"] == "merge":
                merge.add((d["lo"], d["hi"]))
        c = json.loads((run / "clusters.json").read_text())
    cp = set()
    ids = set()
    for m in c["clusters"].values():
        m = sorted(m); ids |= set(m)
        for i, a in enumerate(m):
            for b in m[i + 1:]:
                cp.add((a, b))
    return {"merge": merge, "zones": zones, "cp": cp, "groups": len(c["clusters"]),
            "conflicts": c["stats"]["conflicts_by_invariant"],
            "repart": c["stats"]["n_components_repartitioned"]}

base, arm = load(sys.argv[1]), load(sys.argv[2])
ids = None
labels = None
out = {"base": {k: v for k, v in base.items() if k in ("zones", "groups", "conflicts", "repart")},
       "arm": {k: v for k, v in arm.items() if k in ("zones", "groups", "conflicts", "repart")},
       "merge_base": len(base["merge"]), "merge_arm": len(arm["merge"]),
       "merge_gained": len(arm["merge"] - base["merge"]), "merge_lost": len(base["merge"] - arm["merge"]),
       "cp_base": len(base["cp"]), "cp_arm": len(arm["cp"]),
       "cp_gained": len(arm["cp"] - base["cp"]), "cp_lost": len(base["cp"] - arm["cp"])}
allids = {i for p in base["cp"] | arm["cp"] for i in p}
lab = E.load_labels(set(range(0, 10**9)) if False else allids | {i for p in base["merge"] for i in p})
v = lab["verdict"]
for name, pairs in (("gained", arm["cp"] - base["cp"]), ("lost", base["cp"] - arm["cp"])):
    c = {}
    for k in pairs:
        c[v.get(k, "unlabelled")] = c.get(v.get(k, "unlabelled"), 0) + 1
    out[name + "_labels"] = c
if len(sys.argv) > 3:
    ref = E.load_ref(Path(sys.argv[3]), allids)
    for name, pairs in (("gained", arm["cp"] - base["cp"]), ("lost", base["cp"] - arm["cp"])):
        out[name + "_ref"] = {"cd": len(pairs & ref["cd"]), "cn": len(pairs & ref["cn"])}
    out["ref_base"] = {"cd": len(base["cp"] & ref["cd"]), "cn": len(base["cp"] & ref["cn"]),
                       "cd_total": len(ref["cd"]), "cn_total": len(ref["cn"])}
    out["ref_arm"] = {"cd": len(arm["cp"] & ref["cd"]), "cn": len(arm["cp"] & ref["cn"])}
print(json.dumps(out, indent=1, default=str))
