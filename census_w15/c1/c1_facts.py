"""C1 reader: per fact reader, how often it fires (any / sole) in each of the three readings,
from a baseline replay's fact log. Writes ../<cohort>_fact_fires.json."""
import gzip, json, sys
from collections import Counter, defaultdict
from pathlib import Path
cohort = sys.argv[1]
root = Path("/home/hejtm/autodedup-artifacts/w15/census/c1")
names = json.loads((root / "fact_names.json").read_text())
anyc = {m: Counter() for m in ("gate", "promote", "cluster")}
sole = {m: Counter() for m in ("gate", "promote", "cluster")}
calls = Counter(); nonempty = Counter()
with gzip.open(root / "runs" / f"{cohort}_base_facts.jsonl.gz", "rt") as fh:
    for line in fh:
        r = json.loads(line)
        m = r["mode"]; f = r["facts"]
        calls[m] += 1
        if f:
            nonempty[m] += 1
        for n in set(f):
            anyc[m][n] += 1
        if len(set(f)) == 1:
            sole[m][f[0]] += 1
out = {"calls": dict(calls), "nonempty": dict(nonempty), "readers": {}}
for n in names:
    out["readers"][n] = {m: [anyc[m][n], sole[m][n]] for m in anyc}
(root / f"{cohort}_fact_fires.json").write_text(json.dumps(out, indent=1))
print(calls, nonempty)
never = [n for n in names if all(anyc[m][n] == 0 for m in anyc)]
print("never fire:", len(never), never)
for n in sorted(names, key=lambda n: -sum(anyc[m][n] for m in anyc)):
    v = out["readers"][n]
    if any(x[0] for x in v.values()):
        print(f"{n:<24} gate {v['gate']}  promote {v['promote']}  cluster {v['cluster']}")
