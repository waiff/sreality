"""Model-merge carrier with ABSENT-feature terms split out as MISSING (the E12 caveat).

    python3 missing_share.py <run_dir>
"""
import collections, gzip, json, sys
from pathlib import Path
WT = "/home/hejtm/dev/sreality/.claude/worktrees/w15-c6"
sys.path.insert(0, WT)
from autodedup.features import FAMILY_OF  # noqa: E402
from autodedup.model import LogisticModel  # noqa: E402

model = LogisticModel.from_json((Path(WT) / "autodedup/models/w6_gold.json").read_text())
c_all, c_pres = collections.Counter(), collections.Counter()
miss_share, n = 0.0, 0
flip = collections.Counter()
for line in gzip.open(Path(sys.argv[1]) / "pairs.jsonl.gz", "rt"):
    r = json.loads(line)
    if r["zone"] != "merge" or not (r.get("reason") or "").startswith("model"):
        continue
    feats = {k: (float(v[0]), bool(v[1])) for k, v in (r.get("feats") or {}).items()}
    a, p = collections.defaultdict(float), collections.defaultdict(float)
    for name, v in model.contributions(feats).items():
        parts = name.split("*") if "*" in name else [name.split(":")[0]]
        for base in parts:
            share = v / len(parts)
            fam = FAMILY_OF.get(base, "NONE")
            a[fam] += share
            present = feats.get(base, (0.0, False))[1]
            p[fam if present else "MISSING"] += share
    ca = max(a, key=a.get); cp = max(p, key=p.get)
    c_all[ca] += 1; c_pres[cp] += 1
    pos = {k: v for k, v in p.items() if v > 0}
    miss_share += pos.get("MISSING", 0.0) / (sum(pos.values()) or 1.0); n += 1
    if ca != cp:
        flip[(ca, cp)] += 1
print("model merges", n)
print("carrier (absent terms in their family):", dict(c_all))
print("carrier (absent terms -> MISSING):     ", dict(c_pres))
print(f"mean positive log-odds share of ABSENT-feature terms: {miss_share / n * 100:.1f}%")
print("changed carriers:", dict(flip.most_common(8)))
