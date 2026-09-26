"""Build one cohort's engine, run FULL, verify it against the stored generation, check the
vectorised score against the model's own, and time the phases.

    python3 c2_base.py trial [stored_pairs.jsonl.gz]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np  # noqa: E402

import c2lib  # noqa: E402

OUT = Path("/home/hejtm/autodedup-artifacts/w15/census/c2")


def main() -> None:
    name = sys.argv[1]
    stored = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    eng = c2lib.Engine.build(name, OUT / "cache")
    clock = time.perf_counter()
    run = eng.baseline()
    print(f"[{name}] FULL decided+clustered in {time.perf_counter()-clock:.0f}s "
          f"(cluster {run.seconds:.0f}s)", flush=True)
    cols = c2lib.terms(eng.model, eng.V, eng.P)
    vec = c2lib.score_from_terms(eng.model, cols, n=len(eng.keys))
    py = np.array([d.score if d.zone != "veto" else np.nan for d in run.decisions])
    mask = ~np.isnan(py)
    report = {
        "cohort": name,
        "n_listings": len(eng.ds.listings),
        "pairs": len(eng.keys),
        "zones": {z: sum(1 for d in run.decisions if d.zone == z)
                  for z in ("merge", "band", "reject", "veto")},
        "groups": sum(1 for v in run.clusters.values() if len(v) > 1),
        "cluster_stats": run.stats,
        "vector_score_max_abs_diff": float(np.nanmax(np.abs(vec[mask] - py[mask]))),
        "vector_class_mismatch": int(np.sum(
            c2lib.score_class(eng, vec)[mask] != c2lib.score_class(eng, py)[mask])),
    }
    if stored is not None:
        report["verify"] = c2lib.verify_against(eng, stored)
    ops = c2lib.load_operator(eng)
    report["operator"] = {k: len(v) for k, v in ops.items()}
    report["operator_together"] = {k: c2lib.together(run, v) for k, v in ops.items()}
    (OUT / f"base_{name}.json").write_text(json.dumps(report, indent=1, sort_keys=True))
    print(json.dumps(report, indent=1, sort_keys=True)[:4000], flush=True)


if __name__ == "__main__":
    main()
