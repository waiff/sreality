"""Which rung decided each FULL merge, and which measurement family carried it.

    python3 c2_attrib.py <cohort>

Rung: the certificate, the model cut, the D43 promotion (by warrant), E63. Carrying family of a
model merge: the family whose log-odds contribution sits furthest ABOVE its cohort mean (the
same mean-ablation baseline the family arms use). A promotion is carried by its (B) evidence
read off the features (photo = a tight shared frame, body = containment >= 0.80, code).
"""
from __future__ import annotations

import gzip
import json
import pickle
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np  # noqa: E402

import c2lib  # noqa: E402
from c2lib import FAMILY_OF, FIDX  # noqa: E402
from autodedup.harness import load_model  # noqa: E402

OUT = Path("/home/hejtm/autodedup-artifacts/w15/census/c2")
FAMS = ("ATTR", "PRICE", "TXT", "BRK", "LOC", "IMG", "TIME")


def main() -> None:
    cohort = sys.argv[1]
    with open(OUT / "cache" / f"feats_{cohort}.pkl", "rb") as handle:
        keys, probes, V, P = pickle.load(handle)
    index = {k: i for i, k in enumerate(keys)}
    model = load_model(str(c2lib.MODEL))
    cols = c2lib.terms(model, V, P)
    fam_contrib = {f: np.zeros(len(keys)) for f in FAMS}
    for key, col in cols.items():
        kind, rest = key.split(":", 1)
        names = rest.split("*")
        share = 1.0 / len(names)
        for name in names:
            fam_contrib[FAMILY_OF[name]] += share * col
    excess = {f: fam_contrib[f] - fam_contrib[f].mean() for f in FAMS}
    rows = [json.loads(x) for x in gzip.open(OUT / f"readers_{cohort}" / "full_decisions.jsonl.gz", "rt")]
    rung = Counter()
    carried = Counter()
    carried_by_rung: dict[str, Counter] = defaultdict(Counter)
    model_excess_sum = {f: 0.0 for f in FAMS}
    for lo, hi, zone, reason, cert, score, fams, pre_zone, pre_reason in rows:
        if zone != "merge":
            continue
        i = index[(lo, hi)]
        if cert and not reason.startswith("d43_promote") and not reason.startswith("context_rule"):
            r = f"certificate {cert}"
            fam = {"K-B": "TXT+BRK+TIME", "K-C": "IMG", "K-R": "TXT(code)"}[cert]
        elif reason.startswith("d43_promote"):
            r = "promotion " + reason.split(":", 1)[1].split(":")[0]
            if P[i, FIDX["phash_tight_matches"]] and V[i, FIDX["phash_tight_matches"]] >= 1:
                fam = "IMG(photo)"
            elif P[i, FIDX["containment_max"]] and V[i, FIDX["containment_max"]] >= 0.8:
                fam = "TXT(body)"
            elif P[i, FIDX["ref_code_shared"]]:
                fam = "TXT(code)"
            else:
                fam = "twin/other"
        elif reason.startswith("context_rule"):
            r, fam = "context_rule", "TXT+PRICE"
        else:
            r = "model"
            ex = {f: float(excess[f][i]) for f in FAMS}
            fam = max(ex, key=ex.get)
            for f in FAMS:
                model_excess_sum[f] += max(ex[f], 0.0)
        rung[r] += 1
        carried[fam] += 1
        carried_by_rung[r][fam] += 1
    total_pos = sum(model_excess_sum.values()) or 1.0
    report = {
        "cohort": cohort, "merges": sum(rung.values()), "by_rung": dict(rung),
        "carried_by": dict(carried),
        "carried_by_rung": {k: dict(v) for k, v in carried_by_rung.items()},
        "model_merges_positive_excess_share": {f: round(v / total_pos, 4)
                                               for f, v in model_excess_sum.items()},
    }
    (OUT / f"attrib_{cohort}.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
