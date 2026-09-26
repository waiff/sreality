"""Is D43's promotion WARRANT (E131 agree:2 / photo / unit) load-bearing once D50's demonstration runs?

For every stored band pair whose reason is plain `model` (not promoted, not refused), recompute:
no distinguishing fact (strict) AND no warrant AND the demonstration passes -> the pair would
promote if the warrant step were deleted. Also: of the promoted `agree:2` pairs, how many rest on
`price` + one other attribute merely STATED on both sides.

    python3 warrant_probe.py <run_dir> <cohort.jsonl.gz>
"""
import collections, gzip, json, sys
from pathlib import Path
WT = "/home/hejtm/dev/sreality/.claude/worktrees/w15-c6"
sys.path.insert(0, WT)
from autodedup.dataset import load  # noqa: E402
from autodedup.decide import demonstration_refusal  # noqa: E402
from autodedup.indistinguishable import agreeing_attributes, distinguishing_facts, promotion_warrant  # noqa: E402
from autodedup.settings import Settings  # noqa: E402

settings = Settings.from_json(Path(WT) / "autodedup/settings/w31.json")
ds = load(sys.argv[2])
L = ds.listings
out = collections.Counter()
agree_sets = collections.Counter()
for line in gzip.open(Path(sys.argv[1]) / "pairs.jsonl.gz", "rt"):
    r = json.loads(line)
    reason = r.get("reason") or ""
    feats = {k: (float(v[0]), bool(v[1])) for k, v in (r.get("feats") or {}).items()}
    a, b = L.get(r["lo"]), L.get(r["hi"])
    if a is None or b is None:
        continue
    if reason.startswith("d43_promote:agree"):
        attrs = agreeing_attributes(a, b, feats, settings)
        agree_sets[tuple(sorted(attrs))[:9]] += 1
        out["promoted_agree"] += 1
        out["promoted_agree_price_in_set"] += "price" in attrs
        out["promoted_agree_exactly_2"] += len(attrs) == 2
        continue
    if r["zone"] != "band" or reason != "model":
        continue
    out["band_model"] += 1
    if distinguishing_facts(a, b, feats, settings):
        out["band_model_has_fact"] += 1
        continue
    if promotion_warrant(a, b, feats, settings) is not None:
        out["band_model_warrant_but_not_promoted?"] += 1
        continue
    ref = demonstration_refusal(a, b, feats, settings)
    out["no_fact_no_warrant"] += 1
    out["no_fact_no_warrant_demo_" + ("passes" if ref is None else "refuses:" + ref.split(":")[0])] += 1
print(json.dumps(out, indent=1, sort_keys=True))
print("top agreeing-attribute sets of agree promotions:")
for k, n in agree_sets.most_common(8):
    print(n, k)
