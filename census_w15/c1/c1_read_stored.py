"""C1 reader: fire counts of every decision rule off STORED runs (run.json / clusters.json /
pairs.jsonl.gz), no engine re-run. Writes ../stored_fires.json.

    python3 c1_read_stored.py
"""
from __future__ import annotations

import gzip
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

S15 = Path("/home/hejtm/autodedup-artifacts/w14/s15")
RUNS = {
    "g15_trial_w31": S15 / "score_g15/autodedup-score-36244048665",
    "c17_w30": S15 / "cohort17_runs/w30",
    "c17_w30p": S15 / "cohort17_runs/w30p",
    "c18_w31": S15 / "cohort18_runs/w31",
    "c18_w29": S15 / "cohort18_runs/w29",
}
OUT = Path("/home/hejtm/autodedup-artifacts/w15/census/c1/stored_fires.json")


def bucket(reason: str) -> list[str]:
    """Every rule a terminal reason string names (a reason can carry a certificate AND the
    D43 demotion that overrode it)."""
    out = []
    parts = reason.split(":")
    if reason.startswith("guard:"):
        out.append(f"veto:{parts[1]}")
    elif reason.startswith("auto_reject:"):
        out.append(f"auto_reject:{parts[1]}")
    elif reason.startswith("certificate:"):
        out.append(f"cert_fired:{parts[1]}")
        if len(parts) == 2:
            out.append(f"merge_by:{parts[1]}")
    elif reason.startswith("d43_promote:"):
        out.append("merge_by:D43_promote")
        out.append("D43_promote:" + ":".join(parts[1:]))
    elif reason.startswith("context_rule"):
        out.append("E63:" + ":".join(parts[1:]))
    elif reason == "model":
        out.append("model_zone")
    if ":d43_gate:" in reason:
        out.append("D43_gate_demote")
        out.append("D43_gate_first_fact:" + reason.split(":d43_gate:")[1])
    if ":d43_demonstrate:" in reason:
        out.append("D50_refuse:" + reason.split(":d43_demonstrate:")[1])
    for gate in ("evidence_gate", "stratum_propose_only", "developer_signature",
                 "developer_colive", "unit_evidence_gate", "policy_hold", "kb_family",
                 "dev_hold"):
        if gate in parts:
            out.append(f"gate:{gate}")
    return out


def read(run: Path) -> dict:
    r = json.loads((run / "run.json").read_text())
    c = json.loads((run / "clusters.json").read_text())
    fires: Counter = Counter()
    for reason, n in r["reasons"].items():
        for b in bucket(reason):
            fires[b] += n
    gate_any, gate_sole = Counter(), Counter()
    demo_by_cert = Counter()
    model_merges = 0
    band_scores = []
    with gzip.open(run / "pairs.jsonl.gz", "rt") as fh:
        for line in fh:
            row = json.loads(line)
            ev = row.get("evidence") or {}
            facts = ev.get("d43_gate_facts")
            if facts:
                names = facts.split(",")
                for n in set(names):
                    gate_any[n] += 1
                if len(set(names)) == 1:
                    gate_sole[names[0]] += 1
            if row["zone"] == "merge" and row["reason"] == "model":
                model_merges += 1
            if row["zone"] == "merge" and row["reason"].startswith("d43_promote"):
                demo_by_cert[ev.get("d43_banded_as", "?")] += 1
    st = c["stats"]
    return {
        "n_listings": r["n_listings"],
        "pairs_scored": r["pairs_scored"],
        "zones": r["zones"],
        "certificates": r["certificates"],
        "walls_at_blocking": r["blocking"].get("guarded_pairs"),
        "blocking": {k: r["blocking"].get(k) for k in ("n_pairs", "listings_at_cap",
                                                       "listings_with_zero_candidates",
                                                       "exploded_keys_per_probe",
                                                       "pairs_per_probe")},
        "fires": dict(sorted(fires.items())),
        "d43_gate_fact_any": dict(gate_any.most_common()),
        "d43_gate_fact_sole": dict(gate_sole.most_common()),
        "promoted_from": dict(demo_by_cert.most_common()),
        "cluster": {k: st.get(k) for k in ("n_clusters", "n_clustered_listings", "n_merge_edges",
                                           "n_edges_refused", "conflicts_by_invariant",
                                           "n_components_repartitioned", "n_bridges_applied",
                                           "n_bridges_refused", "max_size", "n_must_link",
                                           "n_must_link_closures", "n_must_not_link")},
        "must_not_link": r.get("must_not_link"),
        "must_link": r.get("must_link"),
        "settings_file": r.get("settings_file"),
        "family_guard": r.get("family_guard"),
        "development_hold": r.get("development_hold"),
        "evidence_families": r.get("evidence_families"),
    }


def main() -> None:
    out = {name: read(path) for name, path in RUNS.items() if (path / "run.json").exists()}
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True))
    for name, v in out.items():
        print(name, v["zones"], v["cluster"]["conflicts_by_invariant"])


if __name__ == "__main__":
    main()
