"""C1: write /home/hejtm/autodedup-artifacts/w15/census/C1_rules.md from the census data files.

Numbers are read from the data files at run time (re-run after new arms land); the prose is here.
"""
import json
from pathlib import Path

ROOT = Path("/home/hejtm/autodedup-artifacts/w15/census/c1")
OUT = Path("/home/hejtm/autodedup-artifacts/w15/census/C1_rules.md")

stored = json.loads((ROOT / "stored_fires.json").read_text())
G15, C18 = stored["g15_trial_w31"], stored["c18_w31"]
C17W30 = stored["c17_w30"]
R17 = json.loads((ROOT / "c17_replay_fires.json").read_text())["fires"]
C17CL = json.loads((ROOT / "runs" / "c17_base_clusters.json").read_text())["stats"]
APPLY = json.loads((ROOT / "apply_fires.json").read_text())


def jl(path):
    p = ROOT / path
    return json.loads(p.read_text()) if p.exists() else None


ARMS = {c: (jl(f"{c}_ablation.json") or {}).get("arms", {}) for c in ("trial", "c17", "c18", "trialr")}
BASE = {c: (jl(f"{c}_ablation.json") or {}).get("base", {}) for c in ("trial", "c17", "c18", "trialr")}
FF = {c: (jl(f"{c}_fact_fires.json") or {}).get("readers", {}) for c in ("trial", "c17", "c18")}


def f3(key, src="f"):
    """fires g15 / c17 / c18"""
    vals = []
    for c in ("g15", "c17", "c18"):
        if src == "f":
            f = G15["fires"] if c == "g15" else (R17 if c == "c17" else C18["fires"])
            v = sum(n for k, n in f.items() if k == key or (key.endswith("*") and k.startswith(key[:-1])))
        elif src == "cl":
            cl = (G15["cluster"]["conflicts_by_invariant"] if c == "g15" else
                  C17CL["conflicts_by_invariant"] if c == "c17" else
                  C18["cluster"]["conflicts_by_invariant"])
            v = cl.get(key, 0)
        elif src == "wall":
            w = (G15 if c == "g15" else C17W30 if c == "c17" else C18)["walls_at_blocking"]
            v = w.get(key, 0)
        else:
            v = 0
        vals.append(f"{v:,}")
    return " / ".join(vals)


def d(arm, cohorts=("trial", "c17")):
    """ablation delta, trial ‖ c17 (‖ c18 when run)"""
    out = []
    for c in cohorts:
        a = ARMS[c].get(arm)
        if a is None:
            out.append("·")
            continue
        if "invalid" in a:
            out.append("invalid")
            continue
        s = f"m{a['dmerge']:+d} g{a['dgroups']:+d} cp+{a['cp_gained']}/−{a['cp_lost']}"
        extra = []
        if a["dsame"]:
            extra.append(f"opSame{a['dsame']:+d}")
        if a["ddiff"]:
            extra.append(f"opDiff{a['ddiff']:+d}")
        gl, ll = a.get("gained_labels", {}), a.get("lost_labels", {})
        if "ref_cd" in gl or "ref_cd" in ll:
            if gl.get("ref_cd", 0) or ll.get("ref_cd", 0) or gl.get("ref_cn", 0) or ll.get("ref_cn", 0):
                extra.append(f"CD+{gl.get('ref_cd', 0)}/−{ll.get('ref_cd', 0)} CN+{gl.get('ref_cn', 0)}")
        out.append(s + ((" " + " ".join(extra)) if extra else ""))
    if any(ARMS[c].get(arm) for c in ("c18",)) and "c18" not in cohorts:
        a = ARMS["c18"][arm]
        if "invalid" not in a:
            out.append(f"c18: m{a['dmerge']:+d} g{a['dgroups']:+d} cp+{a['cp_gained']}/−{a['cp_lost']}")
    return " ‖ ".join(out)


def ref(arm):
    """trial against the label-free g13 reference (trialr)"""
    a = ARMS["trialr"].get(arm)
    if not a or "invalid" in a:
        return "·"
    gl, ll = a.get("gained_labels", {}), a.get("lost_labels", {})
    return f"CD+{gl.get('ref_cd', 0)}/−{ll.get('ref_cd', 0)} CN+{gl.get('ref_cn', 0)}/−{ll.get('ref_cn', 0)}"


def fr(name):
    """one reader's fires: any/sole per reading, trial | c17 | c18"""
    cells = []
    for c in ("trial", "c17", "c18"):
        r = FF[c].get(name)
        if not r:
            cells.append("·")
            continue
        cells.append(" ".join(f"{r[m][0]}/{r[m][1]}" for m in ("gate", "promote", "cluster")))
    return " \\| ".join(cells)


if __name__ == "__main__":
    import sys
    exec(compile(Path(sys.argv[1]).read_text(), sys.argv[1], "exec"))
