"""Reader census on the sixteen earlier cohorts, from their stored w31 (S15b) decisions — no
features recomputed. Populations: GATE = final merges not made by promotion + gate-demoted
bands; PROMOTE = every other band + promoted merges; CLUSTER = every member pair of each
connected component of merge edges. feats=None, so the two image facts (floorplan, interior)
cannot fire here and the cellar photo yield never applies (both read feature slots).

    python3 c2_history.py cohort3 [cohort4 ...]
"""
from __future__ import annotations

import gzip
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import c2lib  # noqa: E402,F401  (installs nothing here; kept for the sys.path)
from autodedup.dataset import load  # noqa: E402
from autodedup.indistinguishable import CLUSTER, GATE, PROMOTE  # noqa: E402
from autodedup.settings import Settings  # noqa: E402

ART = Path("/home/hejtm/autodedup-artifacts/w14")
OUT = Path("/home/hejtm/autodedup-artifacts/w15/census/c2/history")
DF = c2lib._ORIGINAL_DF


def components(edges: list[tuple[int, int]]) -> list[list[int]]:
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups: dict[int, list[int]] = {}
    for x in list(parent):
        groups.setdefault(find(x), []).append(x)
    return [sorted(v) for v in groups.values() if len(v) > 1]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) > 2:
        # One process per cohort: the readers' module-level memos (indistinguishable._SHINGLE_MEMO,
        # text_facts lru caches) grow across cohorts and would not be returned to the OS.
        import subprocess
        for cohort in sys.argv[1:]:
            if not (OUT / f"{cohort}.json").is_file():
                subprocess.run([sys.executable, __file__, cohort], check=False)
        return
    settings = Settings.from_json(c2lib.SETTINGS)
    for cohort in sys.argv[1:]:
        path = OUT / f"{cohort}.json"
        if path.is_file():
            continue
        clock = time.perf_counter()
        if cohort in c2lib.COHORTS:
            # The three unseen cohorts, read on the SAME populations from this census's FULL runs,
            # so dev-cohort and unseen-cohort firing rates are comparable.
            ds = load(str(c2lib.COHORTS[cohort]))
            rows = [r[:4] for r in (json.loads(x) for x in gzip.open(
                OUT.parent / f"readers_{cohort}" / "full_decisions.jsonl.gz", "rt"))]
        else:
            ds = load(str(ART / cohort / "export" / "cohort.jsonl.gz"))
            rows = json.load(gzip.open(ART / "s15" / "runs" / cohort / "S15b" / "decisions.json.gz", "rt"))
        gate, promote, merges = [], [], []
        for lo, hi, zone, reason in rows:
            if zone == "merge":
                merges.append((lo, hi))
                (promote if reason.startswith("d43_promote") else gate).append((lo, hi))
            elif zone == "band":
                (gate if ":d43_gate:" in reason else promote).append((lo, hi))
        counts = {mode: Counter() for mode in ("gate", "promote", "cluster")}
        sole = {mode: Counter() for mode in ("gate", "promote", "cluster")}
        L = ds.listings

        def tally(mode_name, mode, pairs):
            for lo, hi in pairs:
                a, b = L.get(lo), L.get(hi)
                if a is None or b is None:
                    continue
                names = [f.name for f in DF(a, b, None, settings, mode)]
                for n in set(names):
                    counts[mode_name][n] += 1
                if len(names) == 1:
                    sole[mode_name][names[0]] += 1

        tally("gate", GATE, gate)
        tally("promote", PROMOTE, promote)
        member_pairs = []
        for comp in components(merges):
            if len(comp) > 120:
                continue
            member_pairs += [(a, b) for i, a in enumerate(comp) for b in comp[i + 1:]]
        tally("cluster", CLUSTER, member_pairs)
        report = {"cohort": cohort, "n_listings": len(L), "gate_pairs": len(gate),
                  "promote_pairs": len(promote), "cluster_pairs": len(member_pairs),
                  "fires": {m: dict(c) for m, c in counts.items()},
                  "sole": {m: dict(c) for m, c in sole.items()},
                  "seconds": time.perf_counter() - clock}
        path.write_text(json.dumps(report, indent=1, sort_keys=True))
        print(f"{cohort}: {len(L)} listings, gate {len(gate)} promote {len(promote)} "
              f"cluster {len(member_pairs)} in {report['seconds']:.0f}s", flush=True)
        del ds


if __name__ == "__main__":
    main()
