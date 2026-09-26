"""C5(c): training-label corpus vs the cohorts the bars/yardstick are read on; label balance per tier."""
import gzip, json, sys, collections, glob
from pathlib import Path
WT = "/home/hejtm/dev/sreality/.claude/worktrees/w15-c5-substrate"
sys.path.insert(0, WT)
from autodedup.labels import load_all_judgements, labels_by_tier, label_pairs, pair_key, TIER_PRECEDENCE

A = Path("/home/hejtm/autodedup-artifacts")
RUNS7 = ["35124676256", "35116313682", "35115036274", "35155058503", "35149028995", "35202925670", "35203586251"]
EXCL = ["35185988862", "35205840437"]
out = {}

def listing_ids(path):
    ids = set(); blocks = collections.Counter()
    with gzip.open(path, 'rt') as f:
        for line in f:
            if line.startswith('{"t": "listing"'):
                r = json.loads(line); ids.add(int(r['id'])); blocks[r.get('block')] += 1
            elif line.startswith('{"t": "image"'):
                break
    return ids, blocks

# training corpus
j7 = load_all_judgements([A / f"w6/labelfiles/{r}.judgements.jsonl" for r in RUNS7])
jx = load_all_judgements([A / f"w6/labelfiles/{r}.judgements.jsonl" for r in EXCL])
tiers = labels_by_tier(j7)
lab = label_pairs(j7) if True else None
print('judgement rows', len(j7), 'excluded rows', len(jx))
tier_stats = {}
for t, d in tiers.items():
    c = collections.Counter(l.y for l in d.values())
    tier_stats[t] = {"pairs": len(d), "pos": c.get(1, 0), "neg": c.get(0, 0), "abstain": c.get(None, 0)}
print('by tier', tier_stats)
out['tier_stats'] = tier_stats
merged = lab
cy = collections.Counter(l.y for l in merged.values())
ctier = collections.Counter((l.tier, l.y) for l in merged.values())
print('after precedence', len(merged), cy, sorted(ctier.items(), key=str))
out['precedence'] = {"pairs": len(merged), "y": {str(k): v for k, v in cy.items()},
                     "tier_y": {f"{k[0]}|{k[1]}": v for k, v in ctier.items()}}
strata = collections.Counter()
for row in j7:
    strata[str(getattr(row, 'stratum', '') or '').split('|')[0]] += 1
out['judgement_zone_strata'] = dict(strata)
train_pairs = {k for k, l in merged.items() if l.y is not None}
train_listings = {x for k in train_pairs for x in k}
print('labelled pairs (non-abstain)', len(train_pairs), 'listings', len(train_listings))
out['train'] = {"pairs": len(train_pairs), "listings": len(train_listings)}

src_ids, src_blocks = listing_ids(A / "w7/cohort/autodedup-export-35200225251/cohort.jsonl.gz")
out['training_cohort'] = {"listings": len(src_ids), "blocks": dict(src_blocks)}
print('training cohort 35200225251', len(src_ids), dict(src_blocks))

# operator labels
L = A / "w14/labels_g13_36225845749/autodedup-labels-36225845749"
ops = [json.loads(x) for x in open(L / "operator_labels.jsonl")]
mnl = [json.loads(x) for x in open(L / "must_not_link.jsonl")]
opk = {pair_key(r['listing_lo'], r['listing_hi']): r for r in ops}

cohorts = {"trial_g15": A / "w14/s15/score_g15/autodedup-score-36244048665/artifact/cohort.jsonl.gz",
           "c17": A / "w14/s15/cohort17_export_36221961445/autodedup-export-36221961445/cohort.jsonl.gz",
           "c18": A / "w14/s15/cohort18_export_36237638871/autodedup-export-36237638871/cohort.jsonl.gz"}
for c in range(3, 17):
    cohorts[f"c{c}"] = A / f"w14/cohort{c}/export/cohort.jsonl.gz"
reg = glob.glob(str(A / "w14/*region*/**/cohort.jsonl.gz"), recursive=True)
if reg:
    cohorts["region"] = Path(sorted(reg)[0])
rows = {}
for name, path in cohorts.items():
    ids, blocks = listing_ids(path)
    tp_in = [k for k in train_pairs if k[0] in ids and k[1] in ids]
    op_in = [k for k in opk if k[0] in ids and k[1] in ids]
    op_by = collections.Counter((opk[k]['source'], 'same' if opk[k]['verdict'] == 'same' else 'neg') for k in op_in)
    op_in_train = [k for k in op_in if k in train_pairs]
    rows[name] = {"listings": len(ids), "n_blocks": len(blocks),
                  "overlap_training_cohort": len(ids & src_ids),
                  "overlap_training_pair_listings": len(ids & train_listings),
                  "training_pairs_inside": len(tp_in),
                  "operator_pairs_inside": len(op_in),
                  "operator_by_source": {f"{a}|{b}": v for (a, b), v in op_by.items()},
                  "operator_pairs_also_training_pairs": len(op_in_train),
                  "path": str(path)}
    print(name, rows[name], flush=True)
out['cohorts'] = rows
# operator labels vs training labels, globally
both = [k for k in opk if k in merged and merged[k].y is not None]
agree = sum(1 for k in both if (opk[k]['verdict'] == 'same') == (merged[k].y == 1))
out['operator_vs_training_labels'] = {"operator_pairs": len(opk), "also_judged": len(both), "agree": agree,
                                      "disagree_examples": [[k[0], k[1], opk[k]['verdict'], merged[k].tier, merged[k].y]
                                                            for k in both if (opk[k]['verdict'] == 'same') != (merged[k].y == 1)][:20]}
print(out['operator_vs_training_labels'])
json.dump(out, open("/home/hejtm/autodedup-artifacts/w15/census/c5_work/leak.json", "w"), indent=1)
