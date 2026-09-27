"""C1 reader: apply refusal / skip reason counts over every stored trial apply artifact
(live + dry runs), no database. Writes ../apply_fires.json."""
import json
from collections import Counter
from pathlib import Path

PR = Path("/home/hejtm/autodedup-artifacts/w14/prod_readiness")
out = {}
for p in sorted(PR.glob("trial_*/*/apply.json")):
    d = json.loads(p.read_text())
    c = d.get("counts", {})
    refused = Counter()
    for row in d.get("refused", []) or []:
        for r in (row.get("reasons") or [row.get("reason")]):
            refused[str(r)] += 1
    at_apply = Counter()
    for row in d.get("skipped_at_apply", []) or []:
        for r in (row.get("reasons") or [row.get("reason")]):
            at_apply[str(r)] += 1
    out[p.parent.parent.name] = {
        "generation": d.get("generation"), "dry_run": d.get("dry_run"),
        "clusters": c.get("clusters"), "applied": c.get("applied"),
        "already_one_property": c.get("already_one_property"),
        "skipped_by_reason": c.get("skipped_by_reason"),
        "out_of_scope_by_reason": c.get("out_of_scope_by_reason"),
        "refused": dict(refused), "skipped_at_apply": dict(at_apply),
        "failed": len(d.get("failed") or []),
        "deferred_run_cap": c.get("deferred_run_cap"),
        "ruled_different_after_merge": len(d.get("ruled_different_after_merge") or []),
        "legacy_retire_outcomes": (d.get("legacy_retire") or {}).get("counts", {}).get("outcomes"),
    }
tot = Counter()
for k, v in out.items():
    if v["dry_run"]:
        continue
    for name in ("skipped_by_reason", "out_of_scope_by_reason", "refused", "skipped_at_apply"):
        for r, n in (v[name] or {}).items():
            tot[f"{name}:{r}"] += n
    tot["ruled_different_after_merge"] += v["ruled_different_after_merge"]
    tot["deferred_run_cap"] += v["deferred_run_cap"] or 0
    tot["failed"] += v["failed"]
out["_live_totals"] = dict(tot)
Path("/home/hejtm/autodedup-artifacts/w15/census/c1/apply_fires.json").write_text(json.dumps(out, indent=1))
for k, v in out.items():
    print(k, json.dumps(v)[:400])
