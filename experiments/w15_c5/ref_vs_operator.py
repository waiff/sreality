"""C5(c): the engine-derived 'certain duplicate' reference and the engine itself, read against the
operator's rulings (the only independent labels) on the trial (g15), cohort 17 (w30) and cohort 18 (w31)."""
import gzip, json, sys, collections
from pathlib import Path
sys.path.insert(0, "/home/hejtm/dev/sreality/.claude/worktrees/w15-c5-substrate")
from autodedup.dataset import load
from autodedup.guards import pair_veto
from autodedup.text_facts import MAX_CODE_POPULATION, reference_codes, fold
from autodedup.labels import pair_key

A = Path("/home/hejtm/autodedup-artifacts")
L = A / "w14/labels_g13_36225845749/autodedup-labels-36225845749"
ops = {pair_key(r['listing_lo'], r['listing_hi']): r for r in map(json.loads, open(L / "operator_labels.jsonl"))}
mnl = {pair_key(r['listing_lo'], r['listing_hi']) for r in map(json.loads, open(L / "must_not_link.jsonl"))}
RUNS = {
    "trial_g15": (A / "w14/s15/score_g15/autodedup-score-36244048665/artifact/cohort.jsonl.gz",
                  A / "w14/s15/score_g15/autodedup-score-36244048665"),
    "c17_w30": (A / "w14/s15/cohort17_export_36221961445/autodedup-export-36221961445/cohort.jsonl.gz",
                A / "w14/s15/cohort17_runs/w30"),
    "c18_w31": (A / "w14/s15/cohort18_export_36237638871/autodedup-export-36237638871/cohort.jsonl.gz",
                A / "w14/s15/cohort18_runs/w31"),
}
which = sys.argv[1:] or list(RUNS)
res = {}
for name in which:
    cohort, run = RUNS[name]
    ds = load(cohort)
    listings = ds.listings
    by_code, by_text = collections.defaultdict(list), collections.defaultdict(list)
    for l in listings.values():
        for c in reference_codes(l.description):
            by_code[c].append(l.id)
        t = " ".join(fold(l.description or "").split())
        if len(t) >= 300:
            by_text[t].append(l.id)
    carriers = collections.defaultdict(list)
    for lid, gallery in ds.images_by_listing.items():
        for im in gallery:
            if im.phash is None or (im.pop is not None and im.pop >= 8):
                continue
            carriers[im.phash].append(lid)
    frames = collections.Counter()
    for c in carriers.values():
        m = sorted(set(c))
        if 2 <= len(m) <= 12:
            for i, a in enumerate(m):
                for b in m[i + 1:]:
                    frames[(a, b)] += 1
    def idx(index, cap):
        out = set()
        for ids in index.values():
            m = sorted(set(ids))
            if len(m) < 2 or (cap and len(m) > cap):
                continue
            for i, a in enumerate(m):
                for b in m[i + 1:]:
                    out.add((a, b))
        return out
    cls = {"A_code": idx(by_code, MAX_CODE_POPULATION), "B_frames": {k for k, n in frames.items() if n >= 4},
           "C_text": idx(by_text, None)}
    certain = set()
    for k in set().union(*cls.values()):
        a, b = listings.get(k[0]), listings.get(k[1])
        if a and b and not pair_veto(a, b):
            certain.add(k)
    cl = json.loads((run / "clusters.json").read_text())["clusters"]
    member = {int(i): str(g) for g, v in cl.items() for i in v}
    stored = {}
    with gzip.open(run / "pairs.jsonl.gz", "rt") as f:
        for line in f:
            r = json.loads(line)
            stored[pair_key(r['lo'], r['hi'])] = (r['zone'], r.get('certificate'), r.get('reason'))
    inside = {k: r for k, r in ops.items() if k[0] in listings and k[1] in listings}
    t = collections.Counter()
    ex = collections.defaultdict(list)
    for k, r in inside.items():
        y = 'same' if r['verdict'] == 'same' else 'neg'
        src = r['source']
        is_cert = k in certain
        together = member.get(k[0]) is not None and member.get(k[0]) == member.get(k[1])
        z = stored.get(k, ('unstored', None, None))
        t[(y, 'certain' if is_cert else 'not_certain')] += 1
        t[(y, 'together' if together else 'apart')] += 1
        t[(y, src, 'together' if together else 'apart')] += 1
        t[(y, 'zone', z[0], z[1] or '-')] += 1
        if y == 'neg' and together:
            ex['false_merge'].append([k[0], k[1], r['verdict'], src, z[0], z[1], z[2]])
        if y == 'neg' and is_cert:
            ex['certain_but_operator_neg'].append([k[0], k[1], r['verdict'], sorted(n for n, s in cls.items() if k in s)])
    # certificate share of reference
    cert_eng = collections.Counter()
    for k in certain:
        z = stored.get(k, ('unstored', None, None))
        together = member.get(k[0]) is not None and member.get(k[0]) == member.get(k[1])
        cert_eng[(z[1] or ('none:' + z[0]), together)] += 1
    res[name] = {"listings": len(listings), "certain": len(certain),
                 "certain_by_class": {n: len(s) for n, s in cls.items()},
                 "certain_by_engine_certificate": {f"{a}|{'together' if b else 'apart'}": v for (a, b), v in cert_eng.items()},
                 "operator_inside": len(inside), "must_not_link_inside": sum(1 for k in mnl if k[0] in listings and k[1] in listings),
                 "tally": {"|".join(map(str, k)): v for k, v in sorted(t.items(), key=str)},
                 "examples": {k: v[:25] for k, v in ex.items()}}
    print(name, json.dumps(res[name], indent=1)[:6000], flush=True)
json.dump(res, open(f"/home/hejtm/autodedup-artifacts/w15/census/c5_work/ref_vs_operator_{'_'.join(which)}.json", "w"), indent=1)
