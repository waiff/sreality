"""C1: assemble the rule table (fires g15/c17/c18, ablation deltas trial/c17) from the census
data files. Prints markdown rows; the prose of C1_rules.md is written around them.

    python3 c1_report.py > ../rule_rows.md
"""
import json
import pickle
from collections import Counter
from pathlib import Path

ROOT = Path("/home/hejtm/autodedup-artifacts/w15/census/c1")
stored = json.loads((ROOT / "stored_fires.json").read_text())
g15, c18 = stored["g15_trial_w31"], stored["c18_w31"]
c17w30 = stored["c17_w30"]
rep17 = json.loads((ROOT / "c17_replay_fires.json").read_text())
c17clu = json.loads((ROOT / "runs" / "c17_base_clusters.json").read_text())["stats"]
c18rep = (json.loads((ROOT / "c18_replay_fires.json").read_text())
          if (ROOT / "c18_replay_fires.json").exists() else None)


def arms(cohort):
    p = ROOT / f"{cohort}_ablation.json"
    return json.loads(p.read_text())["arms"] if p.exists() else {}


A = {"trial": arms("trial"), "c17": arms("c17"), "trialr": arms("trialr"), "c18": arms("c18")}


def fire(src, key, cohort):
    if cohort == "g15":
        f, cl = g15["fires"], g15["cluster"]["conflicts_by_invariant"]
        walls, certs = g15["walls_at_blocking"], g15["certificates"]
    elif cohort == "c17":
        f, cl = rep17["fires"], c17clu["conflicts_by_invariant"]
        walls, certs = c17w30["walls_at_blocking"], c17w30["certificates"]
    else:
        f, cl = c18["fires"], c18["cluster"]["conflicts_by_invariant"]
        walls, certs = c18["walls_at_blocking"], c18["certificates"]
    if src == "f":
        return sum(v for k, v in f.items() if k == key or (key.endswith("*") and k.startswith(key[:-1])))
    if src == "cl":
        return cl.get(key, 0)
    if src == "wall":
        return walls.get(key, 0)
    if src == "cert":
        return certs.get(key, 0)
    if src == "zero":
        return 0
    return "n/a"


def fires(spec):
    if spec is None:
        return "n/a / n/a / n/a"
    src, key = spec
    return " / ".join(str(fire(src, key, c)) for c in ("g15", "c17", "c18"))


def delta(arm):
    out = []
    for cohort in ("trial", "c17"):
        a = A[cohort].get(arm) if arm else None
        if a is None:
            out.append("–")
        elif "invalid" in a:
            out.append("invalid")
        else:
            s = f"m{a['dmerge']:+d} g{a['dgroups']:+d} cp+{a['cp_gained']}/−{a['cp_lost']}"
            gl, ll = a.get("gained_labels", {}), a.get("lost_labels", {})
            extra = []
            if a["dsame"]:
                extra.append(f"same{a['dsame']:+d}")
            if a["ddiff"]:
                extra.append(f"diff{a['ddiff']:+d}")
            if "ref_cn" in gl or "ref_cd" in gl:
                extra.append(f"CD+{gl.get('ref_cd', 0)}/−{ll.get('ref_cd', 0)} "
                             f"CN+{gl.get('ref_cn', 0)}/−{ll.get('ref_cn', 0)}")
            out.append(s + (" " + " ".join(extra) if extra else ""))
    return " ‖ ".join(out)


if __name__ == "__main__":
    import sys
    for arm in sys.argv[1:]:
        print(arm, delta(arm))
