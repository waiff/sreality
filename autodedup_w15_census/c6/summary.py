"""Fold attribution.json into the ONE operator table (advert grain, ladder order) + merge carriers.

    python3 summary.py <attribution.json> [<label>]
"""
import collections, json, sys

r = json.load(open(sys.argv[1]))
label = sys.argv[2] if len(sys.argv) > 2 else r["run_dir"]
COLS = ["IMG", "TXT", "ATTR", "PRICE", "LOC", "BRK", "TIME", "OPERATOR", "NONE"]


def row_of(outcome, rung):
    if outcome == "grouped":
        if rung == "must_link":
            return "1 joined: operator ruling (must-link)"
        if rung.startswith("certificate"):
            return "2 joined: certificate (" + rung[12:-1] + ")"
        if rung.startswith("model"):
            return "3 joined: model score >= cut"
        if rung.startswith("d43_promote"):
            return "4 joined: fact layer promotion (no stated difference + unit-grade evidence)"
        if rung.startswith("hold"):
            return "0 held"
        return "9 joined: other " + rung
    if rung.startswith("hold") or rung.startswith("band:hold"):
        return "5 alone: held for photo evidence (lane)"
    if rung.startswith("cluster_refused") or rung == "merge_edge_unclustered":
        return "6 alone: merge edge refused by the group check"
    if "d43_gate" in rung:
        return "7 alone: merge demoted by a stated fact (gate)"
    if "d43_refused" in rung:
        return "8 alone: promotion refused (demonstration A/B)"
    if rung.startswith("band:model"):
        return "9 alone: model band (review)"
    if rung.startswith("reject") or rung.startswith("veto"):
        return "10 alone: rejected / vetoed"
    if rung == "no_stored_pair":
        return "11 alone: never compared, walled, or below store floor"
    return "12 alone: other " + rung


table = collections.defaultdict(collections.Counter)
for outcome, rung, fam, n in r["advert_cells"]:
    fam = "ATTR" if fam.startswith("UNMAPPED") else ("NONE" if fam == "GROUP" else fam)
    table[row_of(outcome, rung)][fam] += n
total = sum(sum(c.values()) for c in table.values())
print(f"### {label}: {r['n_adverts']:,} adverts, {r['n_grouped']:,} grouped\n")
print("| outcome (rung that decided) | " + " | ".join(COLS) + " | adverts | share |")
print("|---|" + "---:|" * (len(COLS) + 2))
for key in sorted(table, key=lambda k: int(k.split(" ")[0])):
    c = table[key]
    t = sum(c.values())
    print(f"| {key.split(' ', 1)[1]} | " + " | ".join(f"{c[f]:,}" if c[f] else "-" for f in COLS)
          + f" | {t:,} | {t / total * 100:.1f}% |")
colt = collections.Counter()
for c in table.values():
    colt.update(c)
print("| **all adverts** | " + " | ".join(f"{colt[f]:,}" for f in COLS) + f" | {total:,} | 100% |")
g = collections.Counter()
for key, c in table.items():
    if " joined:" in " " + key.split(" ", 1)[1][:7] or key.split(" ", 1)[1].startswith("joined"):
        g.update(c)
gt = sum(g.values())
print("| **share of joined adverts** | " + " | ".join(f"{g[f] / gt * 100:.1f}%" if g[f] else "-" for f in COLS)
      + f" | {gt:,} | |")
m = collections.Counter()
for zone, rung, fam, n in r["pair_cells"]:
    if zone == "merge":
        m[fam] += n
mt = sum(m.values())
print("| **share of merge edges (pair grain)** | " + " | ".join(f"{m[f] / mt * 100:.1f}%" if m[f] else "-" for f in COLS)
      + f" | {mt:,} edges | |")
