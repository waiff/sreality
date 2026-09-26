"""Compare two harness runs on one export: pair zones/reasons and the group partition."""
import collections
import gzip
import json
import sys

art, base_dir, var_dir = sys.argv[1], sys.argv[2], sys.argv[3]
L = {}
with gzip.open(art, "rt") as f:
    for line in f:
        r = json.loads(line)
        if r.get("t") == "listing":
            L[r["id"]] = r


def pairs(d):
    out = {}
    with gzip.open(d + "/pairs.jsonl.gz", "rt") as f:
        for line in f:
            p = json.loads(line)
            out[(p["lo"], p["hi"])] = (p.get("zone"), p.get("reason"), p.get("certificate"))
    return out


def groups(d):
    c = json.load(open(d + "/clusters.json"))
    of = {}
    for key, members in c["clusters"].items():
        for m in members:
            of[m] = key
    return of


A, B = pairs(base_dir), pairs(var_dir)
keys = set(A) | set(B)
moves = collections.Counter()
examples = collections.defaultdict(list)
for k in keys:
    za = A.get(k, ("absent", None, None))
    zb = B.get(k, ("absent", None, None))
    if za[0] != zb[0]:
        moves[(za[0], zb[0])] += 1
        la, lb = L.get(k[0], {}), L.get(k[1], {})
        examples[(za[0], zb[0])].append({
            "pair": k, "base": za[1], "var": zb[1],
            "src": (la.get("source"), lb.get("source")),
            "floor": (la.get("floor"), lb.get("floor")),
            "total": (la.get("total_floors"), lb.get("total_floors")),
            "cat": (la.get("category_main"), lb.get("category_main")),
        })
print("stored pairs base", len(A), "variant", len(B))
print("zone moves", dict(moves))
reason_moves = collections.Counter()
for k in keys:
    if k in A and k in B and A[k][0] == B[k][0] and A[k][1] != B[k][1]:
        reason_moves[(A[k][1], B[k][1])] += 1
print("same zone, reason moved", sum(reason_moves.values()), reason_moves.most_common(8))
ga, gb = groups(base_dir), groups(var_dir)
together_a = {k for k in keys if ga.get(k[0]) is not None and ga.get(k[0]) == ga.get(k[1])}
together_b = {k for k in keys if gb.get(k[0]) is not None and gb.get(k[0]) == gb.get(k[1])}
print("stored pairs co-grouped base", len(together_a), "variant", len(together_b),
      "joined", len(together_b - together_a), "separated", len(together_a - together_b))
for mv, ex in examples.items():
    print("==", mv, len(ex))
    for e in ex[:12]:
        print("  ", e)
json.dump({"moves": {f"{a}->{b}": n for (a, b), n in moves.items()},
           "examples": {f"{a}->{b}": ex for (a, b), ex in examples.items()},
           "joined": sorted(together_b - together_a), "separated": sorted(together_a - together_b)},
          open(var_dir + "/cmp_vs_w31.json", "w"), default=str, indent=1)
