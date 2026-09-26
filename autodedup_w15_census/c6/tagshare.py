"""How much of each merge decision rests on the ROOM-TAG-derived features (the part a DINOv3
head switch changes), split from the pHash and CLIP-cosine parts of IMG.

    python3 tagshare.py <run_dir> <out.json>
"""
import collections, gzip, json, sys
from pathlib import Path
WT = "/home/hejtm/dev/sreality/.claude/worktrees/w15-c6"
sys.path.insert(0, WT)
from autodedup.features import FAMILY_OF  # noqa: E402
from autodedup.model import LogisticModel, sigmoid  # noqa: E402

TAG = {"interior_match_ratio", "exterior_match_ratio", "plan_match_ratio", "tag_rooms_both",
       "tag_rooms_private", "tag_tight_matches", "tag_dhash_min", "tag_clip_mean", "tag_clip_min",
       "tag_room_clip_min", "tag_room_clip_min2", "tag_lookalike_share", "floorplan_tight_match",
       "floorplan_conflict"}
CLIPV = {"clip_max_cos", "clip_mean_top3_cos"}


def sub(name):
    fam = FAMILY_OF.get(name, "NONE")
    if fam != "IMG":
        return fam
    return "IMG_tag" if name in TAG else "IMG_clip" if name in CLIPV else "IMG_hash"


run = Path(sys.argv[1])
model = LogisticModel.from_json((Path(WT) / "autodedup/models/w6_gold.json").read_text())
CUT = 0.9788
share = collections.defaultdict(lambda: collections.defaultdict(float))
n = collections.Counter()
pivot_tag = collections.Counter()
pivot_img = collections.Counter()
neg_tag = collections.Counter()
for line in gzip.open(run / "pairs.jsonl.gz", "rt"):
    r = json.loads(line)
    if r["zone"] != "merge":
        continue
    feats = {k: (float(v[0]), bool(v[1])) for k, v in (r.get("feats") or {}).items()}
    reason = r.get("reason") or ""
    rung = ("certificate<" + reason.split(":")[1] + ">" if reason.startswith("certificate:")
            else "d43_promote" if reason.startswith("d43_promote") else "model" if reason.startswith("model") else reason)
    sums = collections.defaultdict(float)
    for name, v in model.contributions(feats).items():
        parts = name.split("*", 1) if "*" in name else [name.split(":")[0]]
        for base in parts:
            sums[sub(base) if feats.get(base, (0.0, False))[1] else "MISSING"] += v / len(parts)
    pos = {k: v for k, v in sums.items() if v > 0}
    tot = sum(pos.values()) or 1.0
    for k, v in pos.items():
        share[rung][k] += v / tot
    n[rung] += 1
    logit = model.score(feats)
    if model.apply_calibration(sigmoid(logit - sums.get("IMG_tag", 0.0))) < CUT and sums.get("IMG_tag", 0) > 0:
        pivot_tag[rung] += 1
    if sums.get("IMG_tag", 0.0) < 0:
        neg_tag[rung] += 1
    img = sums.get("IMG_tag", 0) + sums.get("IMG_clip", 0) + sums.get("IMG_hash", 0)
    if img > 0 and model.apply_calibration(sigmoid(logit - img)) < CUT:
        pivot_img[rung] += 1
out = {r: {"n": n[r], "share": {k: round(v / n[r], 4) for k, v in share[r].items()},
           "tag_pivotal": pivot_tag[r], "img_pivotal": pivot_img[r], "tag_net_negative": neg_tag[r]}
       for r in n}
Path(sys.argv[2]).write_text(json.dumps(out, indent=1, sort_keys=True))
for r in sorted(out):
    s = out[r]["share"]
    print(f"{r:24s} n={out[r]['n']:6d} tag {s.get('IMG_tag',0)*100:5.1f}% clip {s.get('IMG_clip',0)*100:5.1f}% hash {s.get('IMG_hash',0)*100:5.1f}%  tag-pivotal {out[r]['tag_pivotal']:5d}  img-pivotal {out[r]['img_pivotal']:5d} tag-net-negative {out[r]['tag_net_negative']}")
