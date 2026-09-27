"""One arm, one row: the pair zones and who carried each merge, the groups, the move against the
base arm, and the arm read against the operator's rulings and a judged sample. Rows append to
one leaderboard (JSONL) and render as one table. The rulings are the test set; the judged sample
is labelled data only (a model may label, never decide a live merge)."""

from __future__ import annotations

import gzip
import json
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from autodedup.evaluate import DIFFERENT, SAME, read_rulings, wilson_lower
from autodedup.guards import cluster_invariants_ok
from autodedup.indistinguishable import CLUSTER, distinguishing_facts
from autodedup.labels import pair_key
from autodedup.lab.board import (BAND, MERGE, REJECT, VETO, Groups, Outcome, config_id,
                                 relation_parts)
from autodedup.lab.cache import REPO, Cohort

JUDGE_SAME: frozenset[str] = frozenset({"same_property"})
JUDGE_DIFFERENT: frozenset[str] = frozenset({"different_property", "same_building_different_unit"})


@dataclass
class Labels:
    rulings: dict[tuple[int, int], str]
    judges: dict[str, dict[tuple[int, int], str]]
    groups: dict[frozenset[int], str] = field(default_factory=dict)


def read_group_reads(paths: Iterable[str | Path]) -> dict[frozenset[int], str]:
    """The review page's exported reads: one member set per line, the newest read winning."""
    out: dict[frozenset[int], str] = {}
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                if entry.get("kind") == "group_read" and entry.get("verdict") in (SAME, DIFFERENT):
                    out[frozenset(int(x) for x in entry["members"])] = entry["verdict"]
    return out


def load_labels(reg: dict[str, Any]) -> Labels:
    rulings = read_rulings(reg["labels"]) if reg.get("labels") else {}
    judges: dict[str, dict[tuple[int, int], str]] = {}
    for name, path in (reg.get("judges") or {}).items():
        out: dict[tuple[int, int], str] = {}
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            verdict = (row.get("verdict") or {}).get("verdict")
            key = pair_key(row["lo"], row["hi"])
            if verdict in JUDGE_SAME:
                out[key] = SAME
            elif verdict in JUDGE_DIFFERENT:
                out[key] = DIFFERENT
        judges[name] = out
    return Labels(rulings, judges, read_group_reads(reg.get("group_reads") or ()))


def _read(groups: Groups, labels: dict[tuple[int, int], str], ids: set[int]) -> dict[str, Any]:
    member_of = groups.member_of
    ruled = {k: v for k, v in labels.items() if k[0] in ids and k[1] in ids}
    together = {k: v for k, v in ruled.items()
                if k[0] in member_of and member_of[k[0]] == member_of.get(k[1])}
    same_n = sum(1 for v in ruled.values() if v == SAME)
    same_t = sum(1 for v in together.values() if v == SAME)
    diff_n = len(ruled) - same_n
    diff_t = len(together) - same_t
    return {"same_n": same_n, "same_together": same_t, "diff_n": diff_n, "diff_together": diff_t,
            "recall": round(same_t / same_n, 4) if same_n else None,
            "precision_wilson_lower": (round(wilson_lower(same_t, same_t + diff_t), 4)
                                       if same_t + diff_t else None)}


def _count(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def git_head() -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short=8", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def row(c: Cohort, arm: Outcome, base: Outcome | None, labels: Labels) -> dict[str, Any]:
    d, g = arm.decisions, arm.groups
    ids = set(c.ds.listings)
    merges = d.zone == MERGE
    co = g.co_pairs()
    out: dict[str, Any] = {
        "experiment": arm.config.get("name"), "config_id": config_id(arm.config),
        "cohort": c.name, "cache": c.version, "code": git_head(),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "zones": {"merge": int(merges.sum()), "band": int((d.zone == BAND).sum()),
                  "reject": int((d.zone == REJECT).sum()), "veto": int((d.zone == VETO).sum())},
        "merge_by": _count(f"{r}:{n}" if r in ("proof", "context") else r
                           for r, n in zip(d.rung[merges], d.name[merges])),
        "merge_carrier": _count(d.carrier[merges]),
        "groups": len(g.clusters), "grouped": sum(len(v) for v in g.clusters.values()),
        "copairs": len(co),
        "rulings": _read(g, labels.rulings, ids),
        "judges": {name: _read(g, pairs, ids) for name, pairs in labels.judges.items()},
        "timings": {k: round(v, 2) for k, v in arm.timings.items()},
    }
    if base is not None:
        base_co = base.groups.co_pairs()
        arm_sets = {frozenset(v) for v in g.clusters.values()}
        base_sets = {frozenset(v) for v in base.groups.clusters.values()}
        moved: set[int] = set()
        for members in arm_sets ^ base_sets:
            moved |= members
        zone_moves = _count(f"{a}->{b}" for a, b in zip(
            np.array(["u", "veto", "reject", "band", "merge"])[base.decisions.zone],
            np.array(["u", "veto", "reject", "band", "merge"])[d.zone]) if a != b)
        out["vs_base"] = {"base": base.config.get("name"), "zone_moves": zone_moves,
                          "copairs_gained": len(co - base_co), "copairs_lost": len(base_co - co),
                          "groups_arm_only": len(arm_sets - base_sets),
                          "groups_base_only": len(base_sets - arm_sets),
                          "adverts_moved": len(moved)}
        if labels.groups:
            out["group_reads"] = {
                side: {v: sum(1 for m in sets if labels.groups.get(m) == v)
                       for v in (SAME, DIFFERENT)}
                for side, sets in (("arm_only", arm_sets - base_sets),
                                   ("base_only", base_sets - arm_sets))}
    return out


def verify(c: Cohort, outcome: Outcome, stored: str | Path) -> dict[str, Any]:
    """The arm against a stored generation's rows: zone, reason and score, row by row."""
    index = {key: i for i, key in enumerate(c.keys)}
    zone_names = np.array(["undecided", "veto", "reject", "band", "merge"])
    d = outcome.decisions
    n = same = 0
    differ: dict[str, int] = {"zone": 0, "reason": 0, "score": 0, "missing": 0}
    examples: list[Any] = []
    with gzip.open(stored, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            stored_row = json.loads(line)
            n += 1
            i = index.get((stored_row["lo"], stored_row["hi"]))
            if i is None:
                differ["missing"] += 1
                continue
            ok = True
            for field_name, mine, theirs in (
                    ("zone", zone_names[d.zone[i]], stored_row["zone"]),
                    ("reason", d.reason[i], stored_row["reason"])):
                if mine != theirs:
                    differ[field_name] += 1
                    ok = False
            if abs(float(d.score[i]) - float(stored_row["score"])) > 1e-12:
                differ["score"] += 1
                ok = False
            same += ok
            if not ok and len(examples) < 8:
                examples.append({"key": [stored_row["lo"], stored_row["hi"]],
                                 "stored": [stored_row["zone"], stored_row["reason"]],
                                 "lab": [str(zone_names[d.zone[i]]), d.reason[i]]})
    return {"stored_rows": n, "identical": same, "differ": differ, "examples": examples}


def groups_identical(outcome: Outcome, run_dir: str | Path) -> dict[str, Any]:
    """The arm's groups against a `harness run` output (SW1's one path)."""
    raw = json.loads((Path(run_dir) / "clusters.json").read_text(encoding="utf-8"))["clusters"]
    theirs = {frozenset(int(m) for m in v) for v in raw.values() if len(v) > 1}
    mine = {frozenset(v) for v in outcome.groups.clusters.values()}
    return {"lab": len(mine), "harness_run": len(theirs), "identical": mine == theirs,
            "only_lab": len(mine - theirs), "only_harness_run": len(theirs - mine)}


def where_lost(c: Cohort, arm: Outcome, labels: Labels) -> dict[str, dict[str, int]]:
    """Each labelled pair the arm does not put together, by where it was lost: never a candidate
    (retrieval), settled below merge by a rung (the rung and, for a fact, its family), or a merge
    edge the group step refused. The ceiling read: which stage owns the misses."""
    index = {key: i for i, key in enumerate(c.keys)}
    d, member_of = arm.decisions, arm.groups.member_of
    ids = set(c.ds.listings)
    zone_names = ("undecided", "veto", "reject", "band", "merge")
    out: dict[str, dict[str, int]] = {}
    for source, pairs in [("operator", labels.rulings)] + list(labels.judges.items()):
        for verdict in (SAME, DIFFERENT):
            counts: dict[str, int] = {}
            for (a, b), value in pairs.items():
                if value != verdict or a not in ids or b not in ids:
                    continue
                if a in member_of and member_of[a] == member_of.get(b):
                    where = "together"
                else:
                    i = index.get((a, b))
                    if i is None:
                        where = "not a candidate"
                    elif d.zone[i] == MERGE:
                        where = "merge edge, group refused"
                    elif d.rung[i] == "fact":
                        where = f"{zone_names[d.zone[i]]} by fact:{d.carrier[i]}"
                    else:
                        where = f"{zone_names[d.zone[i]]} by {d.rung[i]}"
                counts[where] = counts.get(where, 0) + 1
            out[f"{source}:{verdict}"] = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
    return out


def why_refused(c: Cohort, arm: Outcome, labels: Labels) -> dict[str, dict[str, Any]]:
    """Each labelled pair that is a merge edge the relation group step left apart: the invariant
    that refuses the pair alone and the union of its two groups, and every fact that separates a
    cross pair of that union (`price_limb` when only E157's price read does). The read that traces
    the group step's losses to the facts that cause them."""
    group = arm.config.get("group", {"step": "relation"})
    parts = relation_parts(c, arm.decisions, group)
    vetoed = parts.vetoed | frozenset(tuple(x) for x in group.get("must_not_link", ()))
    index = {key: i for i, key in enumerate(c.keys)}
    d, member_of, clusters = arm.decisions, arm.groups.member_of, arm.groups.clusters

    def members(x: int) -> list[int]:
        g = member_of.get(x)
        return list(clusters[g]) if g is not None else [x]

    def invariant(ids: list[int]) -> str:
        fps = [c.fps[i] for i in ids if i in c.fps]
        return cluster_invariants_ok(fps, parts.settings, vetoed, parts.relation, None) or "ok"

    unlabelled = {c.keys[i]: "edge" for i in np.flatnonzero(d.zone == MERGE)}
    sources = [("operator", labels.rulings)] + list(labels.judges.items())
    out: dict[str, dict[str, Any]] = {}
    for source, pairs, verdicts in [(s, p, (SAME, DIFFERENT)) for s, p in sources] + [
            ("all_refused_edges", unlabelled, ("edge",))]:
        for verdict in verdicts:
            where: Counter[str] = Counter()
            facts: Counter[str] = Counter()
            for (a, b), value in pairs.items():
                if value != verdict or a not in c.ds.listings or b not in c.ds.listings:
                    continue
                i = index.get((a, b))
                if i is None or d.zone[i] != MERGE:
                    continue
                if a in member_of and member_of[a] == member_of.get(b):
                    continue
                left, right = members(a), members(b)
                union = sorted(set(left) | set(right))
                where[f"pair:{invariant([a, b])} group:{invariant(union)}"] += 1
                seen: set[str] = set()
                for x in left if parts.relation is not None else ():
                    for y in right:
                        lo, hi = min(x, y), max(x, y)
                        if parts.relation.ok(lo, hi):
                            continue
                        found = distinguishing_facts(c.ds.listings[lo], c.ds.listings[hi],
                                                     parts.slots.get((lo, hi)), parts.settings,
                                                     CLUSTER)
                        seen |= {f.name for f in found} or {"price_limb"}
                facts.update(seen)
            out[source if verdict == "edge" else f"{source}:{verdict}"] = {
                "where": dict(where.most_common()), "separating_facts": dict(facts.most_common()),
                "n": sum(where.values())}
    return out


# --- the leaderboard ----------------------------------------------------------------------------

def append(board: Path, entry: dict[str, Any]) -> None:
    board.parent.mkdir(parents=True, exist_ok=True)
    with open(board, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _fmt(value: Any) -> str:
    return "–" if value is None else str(value)


def render(board: Path, cohorts: Iterable[str] | None = None) -> str:
    """The one table: newest row per (experiment, cohort, config), cohorts in order."""
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for line in board.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            latest[(entry["cohort"], entry["experiment"], entry["config_id"])] = entry
    wanted = list(cohorts) if cohorts else sorted({k[0] for k in latest})
    lines = ["| cohort | experiment | merge | band | groups | co-pairs | Δco +/− | adverts moved "
             "| op same together | op diff together | op prec. LB | judge(vision) same | "
             "judge(vision) diff | judge(gold) diff | decide s | group s |",
             "|" + "---|" * 16]
    for cohort in wanted:
        rows = sorted((e for k, e in latest.items() if k[0] == cohort),
                      key=lambda e: (e["rulings"]["diff_together"],
                                     -(e["rulings"]["same_together"])))
        for e in rows:
            vs = e.get("vs_base") or {}
            r = e["rulings"]
            jv = e["judges"].get("vision", {})
            jg = e["judges"].get("gold", {})
            lines.append("| " + " | ".join(_fmt(x) for x in (
                cohort, e["experiment"], e["zones"]["merge"], e["zones"]["band"], e["groups"],
                e["copairs"],
                f"+{vs.get('copairs_gained', 0)}/−{vs.get('copairs_lost', 0)}" if vs else None,
                vs.get("adverts_moved") if vs else None,
                f"{r['same_together']}/{r['same_n']}", f"{r['diff_together']}/{r['diff_n']}",
                r["precision_wilson_lower"],
                f"{jv.get('same_together')}/{jv.get('same_n')}" if jv else None,
                f"{jv.get('diff_together')}/{jv.get('diff_n')}" if jv else None,
                f"{jg.get('diff_together')}/{jg.get('diff_n')}" if jg else None,
                e["timings"].get("decide_s"), e["timings"].get("group_s"))) + " |")
    return "\n".join(lines) + "\n"


# --- the review input: the groups an arm changes, with each advert's stated facts --------------

def review(c: Cohort, arm: Outcome, base: Outcome, labels: Labels, limit: int | None = None
           ) -> dict[str, Any]:
    """Every group the arm forms that the base does not, and the reverse (all of them, so a page
    can draw an unbiased sample), each advert with its stated facts and each decided or labelled
    member pair with its decision and its labels: the input of one review page."""
    index = {key: i for i, key in enumerate(c.keys)}
    d = arm.decisions
    arm_sets = {frozenset(v) for v in arm.groups.clusters.values()}
    base_sets = {frozenset(v) for v in base.groups.clusters.values()}

    def advert(i: int) -> dict[str, Any]:
        x = c.ds.listings[i]
        return {k: getattr(x, k, None) for k in ("id", "source", "category_type", "category_main",
                                                 "area_m2", "price", "disposition", "floor",
                                                 "street", "source_url")}

    def card(members: frozenset) -> dict[str, Any]:
        ids = sorted(members)
        pairs = []
        for k, a in enumerate(ids):
            for b in ids[k + 1:]:
                i = index.get((a, b))
                judged = {n: j.get((a, b)) for n, j in labels.judges.items()}
                if i is None and (a, b) not in labels.rulings and not any(judged.values()):
                    continue
                pairs.append({"lo": a, "hi": b,
                              "zone": None if i is None else int(d.zone[i]),
                              "rung": None if i is None else d.rung[i],
                              "name": None if i is None else d.name[i],
                              "carrier": None if i is None else d.carrier[i],
                              "ruling": labels.rulings.get((a, b)),
                              "judge": judged})
        return {"members": [advert(i) for i in ids], "pairs": pairs}
    gained = sorted(arm_sets - base_sets, key=len, reverse=True)[:limit]
    lost = sorted(base_sets - arm_sets, key=len, reverse=True)[:limit]
    return {"experiment": arm.config.get("name"), "base": base.config.get("name"),
            "cohort": c.name, "groups_gained": [card(g) for g in gained],
            "groups_lost": [card(g) for g in lost]}
