"""`lab verify`: the lab reproduces the one path, or no number it prints counts.

Three reads, all against the SAME `harness run --evidence` directory the cohort was read from:
1. rows: the reference ladder's decision for every pair the run decided (stored or not) against the
   run's own, on zone, reason and score (within 1e-6), and its stored set against `pairs.jsonl.gz`;
2. groups: the reference group step against the run's `clusters.json`;
3. rungs: each reference rung against the engine function it restates (`decide._decide_layers`,
   `apply_context_rule`, `apply_d43_rule`, `apply_merge_policy`), pair by pair, rung by rung, so a
   rung that drifts is named even when a later rung happens to mask it.
A passing verify is recorded under the cache root against a STAMP: the artefact's version (the
engine) plus a digest of the lab's own modules (the rungs), so editing a rung un-verifies every
cache until it is verified again. `lab keep` counts only rows whose stamp passed."""

from __future__ import annotations

import gzip
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from autodedup.decide import (
    Decision,
    _decide_layers,
    apply_context_rule,
    apply_d43_rule,
    apply_merge_policy,
)
from autodedup.harness import CLUSTERS_FILE, PAIRS_FILE
from autodedup.lab.board import REFERENCE_LADDER, U, ZONE_NAMES, Outcome, decide, storable
from autodedup.lab.cache import ZONE_CODE, Cohort

SCORE_TOL: float = 1e-6
VERIFIED_FILE: str = "verified.jsonl"
LAB_DIR: Path = Path(__file__).resolve().parent


def lab_digest() -> str:
    """The lab's own code (rungs, group steps, scorers): not part of the artefact's version."""
    digest = hashlib.sha1()
    for path in sorted(LAB_DIR.glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


def stamp(version: str) -> str:
    """What a verify certifies and a leaderboard row carries: the engine's artefact AND the lab."""
    return f"{version}+{lab_digest()}"


def rows(c: Cohort, outcome: Outcome) -> dict[str, Any]:
    """The arm against the run's decision for every pair, and its stored set against the run's."""
    assert c.run is not None
    d, run = outcome.decisions, c.run
    zone = d.zone != run.zone
    reason = d.reason != run.reason
    score = np.abs(d.score - run.score) > SCORE_TOL
    bad = zone | reason | score
    lab_stored = storable(c, d)
    examples = [{"key": list(c.keys[i]), "run": [ZONE_NAMES[run.zone[i]], run.reason[i],
                                                 float(run.score[i])],
                 "lab": [ZONE_NAMES[d.zone[i]], d.reason[i], float(d.score[i])]}
                for i in np.flatnonzero(bad)[:8]]
    stored_file = _stored_rows(c.run.dir / PAIRS_FILE)
    index = {key: i for i, key in enumerate(c.keys)}
    file_bad = 0
    for key, (z, r, s) in stored_file.items():
        i = index.get(key)
        if (i is None or not lab_stored[i] or ZONE_NAMES[d.zone[i]] != z or d.reason[i] != r
                or abs(float(d.score[i]) - s) > SCORE_TOL):
            file_bad += 1
    return {"pairs": c.n, "identical": int(c.n - bad.sum()),
            "differ": {"zone": int(zone.sum()), "reason": int(reason.sum()),
                       "score": int(score.sum())},
            "stored_rows": len(stored_file), "stored_lab": int(lab_stored.sum()),
            "stored_rows_identical": len(stored_file) - file_bad,
            "stored_only_lab": int(lab_stored.sum()) - (len(stored_file) - file_bad),
            "examples": examples}


def _stored_rows(path: Path) -> dict[tuple[int, int], tuple[str, str, float]]:
    out: dict[tuple[int, int], tuple[str, str, float]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                out[(int(row["lo"]), int(row["hi"]))] = (row["zone"], row["reason"],
                                                         float(row["score"]))
    return out


def stored_generation(c: Cohort, outcome: Outcome, path: str | Path) -> dict[str, Any]:
    """The arm against an OLDER stored generation's rows (e.g. g15, census c6), row by row."""
    index = {key: i for i, key in enumerate(c.keys)}
    d = outcome.decisions
    theirs = _stored_rows(Path(path))
    differ = {"zone": 0, "reason": 0, "score": 0, "missing": 0}
    for key, (z, r, s) in theirs.items():
        i = index.get(key)
        if i is None:
            differ["missing"] += 1
            continue
        differ["zone"] += ZONE_NAMES[d.zone[i]] != z
        differ["reason"] += d.reason[i] != r
        differ["score"] += abs(float(d.score[i]) - s) > SCORE_TOL
    same = sum(1 for key, (z, r, s) in theirs.items()
               if key in index and ZONE_NAMES[d.zone[index[key]]] == z
               and d.reason[index[key]] == r and abs(float(d.score[index[key]]) - s) <= SCORE_TOL)
    return {"stored_rows": len(theirs), "identical": same, "differ": differ}


def groups(outcome: Outcome, run_dir: str | Path) -> dict[str, Any]:
    """The arm's groups against a `harness run` output's clusters (SW1's one path)."""
    raw = json.loads((Path(run_dir) / CLUSTERS_FILE).read_text(encoding="utf-8"))["clusters"]
    theirs = {frozenset(int(m) for m in v) for v in raw.values() if len(v) > 1}
    mine = {frozenset(v) for v in outcome.groups.clusters.values()}
    return {"lab": len(mine), "harness_run": len(theirs), "identical": mine == theirs,
            "only_lab": len(mine - theirs), "only_harness_run": len(theirs - mine)}


# --- rung by rung: each reference rung is the engine function it restates -----------------------

def _engine_stages(c: Cohort, i: int) -> dict[str, Decision]:
    """decide_pair's stages for pair i, off the artefact's inputs (feats, census, settings)."""
    lo, hi = c.keys[i]
    fa, fb, la, lb = c.fps[lo], c.fps[hi], c.ds.listings[lo], c.ds.listings[hi]
    feats = c.feats(i)
    layers = _decide_layers(fa, fb, la, lb, feats, c.probes[i], c.model, c.settings)
    context = apply_context_rule(layers, feats, la, lb, c.settings, c.census)
    gate = apply_d43_rule(context, la, lb, feats, c.settings) if context.zone == "merge" else context
    d43 = apply_d43_rule(context, la, lb, feats, c.settings)
    return {"layers": layers, "context": context, "gate": gate, "d43": d43,
            "policy": apply_merge_policy(d43, la, lb, c.settings)}


def _expected(rung: str, stages: dict[str, Decision]) -> Decision | None:
    """What the engine has settled once the reference ladder has run through `rung` (None =
    not yet settled): the rule floor's early returns one by one, then each stage whole."""
    layers = stages["layers"]
    if rung == "veto":
        return layers if layers.zone == "veto" else None
    if rung == "auto_reject":
        return layers if layers.zone == "veto" or layers.reason.startswith("auto_reject:") else None
    if rung == "proof":
        return (layers if layers.zone == "veto" or layers.reason.startswith("auto_reject:")
                or layers.certificate is not None else None)
    return {"score": layers, "context": stages["context"], "gate": stages["gate"],
            "demonstrate": stages["d43"], "policy": stages["policy"]}[rung]


def rung_equivalence(c: Cohort, idx: Sequence[int] | None = None,
                     ladder: Sequence[str] = REFERENCE_LADDER) -> dict[str, Any]:
    """For every rung of the reference ladder: the lab's decisions after the ladder up to and
    including that rung, against what `decide_pair`'s own functions have settled at that point,
    on every pair in `idx` (all by default). `moved` counts the pairs each rung settled or
    re-read, so a fixture that never exercises a rung shows it."""
    pick = np.arange(c.n) if idx is None else np.asarray(idx, dtype=np.int64)
    stages = {int(i): _engine_stages(c, int(i)) for i in pick}
    out: dict[str, Any] = {"pairs": len(pick), "rungs": {}}
    zone_before = np.zeros(c.n, dtype=np.int8)
    reason_before = np.full(c.n, "", dtype=object)
    for k, rung in enumerate(ladder):
        d, _ = decide(c, {"ladder": [{"rung": r} for r in ladder[:k + 1]]}, finish=False)
        bad = 0
        examples: list[Any] = []
        for i in (int(x) for x in pick):
            want = _expected(rung, stages[i])
            if want is None:
                ok = d.zone[i] == U
            else:
                ok = (d.zone[i] == ZONE_CODE[want.zone] and d.reason[i] == want.reason
                      and abs(float(d.score[i]) - float(want.score)) <= SCORE_TOL)
            if not ok:
                bad += 1
                if len(examples) < 8:
                    examples.append({"key": list(c.keys[i]),
                                     "lab": [ZONE_NAMES[d.zone[i]], d.reason[i]],
                                     "engine": None if want is None else [want.zone, want.reason]})
        moved = (d.zone[pick] != zone_before[pick]) | (d.reason[pick] != reason_before[pick])
        out["rungs"][rung] = {"identical": len(pick) - bad, "moved": int(moved.sum()),
                              "examples": examples}
        zone_before, reason_before = d.zone.copy(), d.reason.copy()
    out["ok"] = all(r["identical"] == len(pick) for r in out["rungs"].values())
    return out


# --- the record `lab keep` reads -----------------------------------------------------------------

def record(root: Path, c: Cohort, report: dict[str, Any], ok: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with open(root / VERIFIED_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"cohort": c.name, "cache": c.version, "stamp": stamp(c.version),
                                 "code_digest": c.code_digest, "ok": ok, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                 "rows": report.get("rows", {}).get("identical"),
                                 "groups": report.get("groups", {}).get("harness_run")},
                                sort_keys=True) + "\n")


def verified(root: Path) -> set[str]:
    """The stamps whose newest verify passed."""
    path = root / VERIFIED_FILE
    if not path.is_file():
        return set()
    newest: dict[str, bool] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            newest[entry.get("stamp", "")] = bool(entry["ok"])
    return {key for key, ok in newest.items() if ok and key}


def passed(report: dict[str, Any]) -> bool:
    r, g = report.get("rows"), report.get("groups")
    ok = bool(r) and r["identical"] == r["pairs"] and r["stored_rows_identical"] == r["stored_rows"] \
        and r["stored_only_lab"] == 0
    ok = ok and bool(g) and g["identical"]
    if "rung_equivalence" in report:
        ok = ok and report["rung_equivalence"]["ok"]
    return ok
