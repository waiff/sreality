"""Run w31 + w6_gold over one cohort export from the C6 worktree (origin/main), as run_cohort.py does.

    python3 run_w31.py <cohort.jsonl.gz> <out_dir>
"""
from __future__ import annotations
import json, resource, sys, time
from pathlib import Path
WT = "/home/hejtm/dev/sreality/.claude/worktrees/w15-c6"
sys.path.insert(0, WT)
from autodedup.dataset import load  # noqa: E402
from autodedup.harness import load_model, load_must_not_link, run_engine  # noqa: E402
from autodedup.settings import Settings  # noqa: E402

MNL = Path("/home/hejtm/autodedup-artifacts/w14/data/autodedup-labels-35609425873/must_not_link.jsonl")
cohort, out = Path(sys.argv[1]), Path(sys.argv[2])
ds = load(cohort)
ids = set(ds.listings)
mnl = frozenset(p for p in load_must_not_link(str(MNL)) if p[0] in ids and p[1] in ids)
cfg = Settings.from_json(Path(WT) / "autodedup/settings/w31.json")
model = load_model(str(Path(WT) / "autodedup/models/w6_gold.json"))
clock = time.perf_counter()
summary = run_engine(ds, cfg, model, out, mnl if cfg.operator_must_not_link else frozenset())
summary["timings"]["wall_s"] = time.perf_counter() - clock
summary["artifact"] = str(cohort)
summary["max_rss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
(out / "run.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
print("done", f"{summary['timings']['wall_s']:.0f}s", flush=True)
