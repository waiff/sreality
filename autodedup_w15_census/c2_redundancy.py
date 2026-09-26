"""Feature-matrix census over one cohort's cached pair features (no dataset load):
presence, always-zero / always-missing, pairwise |r| > 0.9 (all pairs and the stored region),
identical presence columns, and each feature's log-odds contribution spread under w6_gold.

    python3 c2_redundancy.py <cohort>
"""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np  # noqa: E402

import c2lib  # noqa: E402
from c2lib import FAMILY_OF, FEATURE_ORDER, FIDX  # noqa: E402
from autodedup.harness import load_model  # noqa: E402

OUT = Path("/home/hejtm/autodedup-artifacts/w15/census/c2")


def corr_pairs(X: np.ndarray, names: list[str], bar: float) -> list[tuple[str, str, float]]:
    std = X.std(axis=0)
    keep = [j for j in range(X.shape[1]) if std[j] > 0]
    Xk = X[:, keep]
    R = np.corrcoef(Xk, rowvar=False)
    out = []
    for a in range(len(keep)):
        for b in range(a + 1, len(keep)):
            r = R[a, b]
            if abs(r) > bar:
                out.append((names[keep[a]], names[keep[b]], round(float(r), 4)))
    return sorted(out, key=lambda t: -abs(t[2]))


def main() -> None:
    cohort = sys.argv[1]
    with open(OUT / "cache" / f"feats_{cohort}.pkl", "rb") as handle:
        keys, probes, V, P = pickle.load(handle)
    model = load_model(str(c2lib.MODEL))
    X = np.where(P, V, 0.0)
    n = len(keys)
    cols = c2lib.terms(model, V, P)
    score = c2lib.score_from_terms(model, cols, n=n)
    region = score >= 0.02
    names = list(FEATURE_ORDER)
    per = {}
    for j, name in enumerate(names):
        x, p = X[:, j], P[:, j]
        contrib = cols.get(f"v:{name}", np.zeros(n)) + cols.get(f"p:{name}", np.zeros(n))
        for key, col in cols.items():
            if key.startswith("i:") and name in key[2:].split("*"):
                contrib = contrib + 0.5 * col
        per[name] = {
            "family": FAMILY_OF[name],
            "present_share": round(float(p.mean()), 4),
            "present_share_region": round(float(p[region].mean()), 4) if region.any() else None,
            "nonzero_share": round(float((x != 0).mean()), 4),
            "distinct_values": int(len(np.unique(x[p]))) if p.any() else 0,
            "mean_present": round(float(x[p].mean()), 4) if p.any() else None,
            "logodds_sd_all": round(float(contrib.std()), 4),
            "logodds_sd_region": round(float(contrib[region].std()), 4) if region.any() else None,
            "logodds_absmean_region": round(float(np.abs(contrib[region] - contrib[region].mean()).mean()), 4)
            if region.any() else None,
        }
    presence_groups: dict[bytes, list[str]] = {}
    for j, name in enumerate(names):
        presence_groups.setdefault(np.packbits(P[:, j]).tobytes(), []).append(name)
    report = {
        "cohort": cohort, "pairs": n, "stored_region_pairs": int(region.sum()),
        "always_missing": [nm for nm in names if not P[:, FIDX[nm]].any()],
        "always_zero": [nm for nm in names if not (X[:, FIDX[nm]] != 0).any()],
        "constant_when_present": [nm for nm in names if per[nm]["distinct_values"] <= 1],
        "corr_gt_0.9_all": corr_pairs(X, names, 0.9),
        "corr_gt_0.9_region": corr_pairs(X[region], names, 0.9),
        "corr_gt_0.8_region": corr_pairs(X[region], names, 0.8),
        "identical_presence_groups": [g for g in presence_groups.values() if len(g) > 1],
        "features": per,
    }
    (OUT / f"redundancy_{cohort}.json").write_text(json.dumps(report, indent=1))
    print(json.dumps({k: v for k, v in report.items() if k != "features"}, indent=1))


if __name__ == "__main__":
    main()
