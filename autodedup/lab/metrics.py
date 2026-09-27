"""One arm, one row: the pair zones and who carried each merge, the groups, the move against the
base arm, and the arm read against the operator's rulings and a judged sample, as GLOBAL_SEARCH
2.2's M1-M9. Rows append to one leaderboard (JSONL) and render as one table file. The rulings are
the test set; the judged sample is labelled data only (a model may label, never decide a live
merge)."""

from __future__ import annotations

import json
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from autodedup.evaluate import DIFFERENT, SAME, read_rulings, wilson_interval, wilson_lower
from autodedup.guards import cluster_invariants_ok
from autodedup.indistinguishable import CLUSTER, distinguishing_facts
from autodedup.labels import pair_key
from autodedup.lab.board import (BAND, MERGE, REJECT, VETO, Groups, Outcome, config_id,
                                 relation_parts)
from autodedup.lab.cache import REPO, Cohort
from autodedup.lab.page import M45_N, M45_SEED, draw

JUDGE_SAME: frozenset[str] = frozenset({"same_property"})
JUDGE_DIFFERENT: frozenset[str] = frozenset({"different_property", "same_building_different_unit"})
KEEP_APART = Path(__file__).with_name("keep_apart.json")


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


def load_fixtures(path: Path = KEEP_APART) -> list[tuple[str, int, int, str]]:
    """The keep-apart fixtures (hard bar M3 / A3) as (cohort, lo, hi, case)."""
    return [(row["cohort"], row["ids"][0], row["ids"][1], row["case"])
            for row in json.loads(path.read_text(encoding="utf-8"))["fixtures"]]


def fixtures_apart(groups: Groups, fixtures: Iterable[tuple[str, int, int, str]], cohort: str,
                   ids: set[int]) -> dict[str, Any]:
    """M3 on every fixture whose two adverts are in the cohort, whatever cohort the file names it under.

    A fixture listed under `cohort`, or with one advert in it, that is not whole here raises: the export
    dropped or renumbered a fixture advert (G2's `fixtures.present` counts and fails the same way)."""
    absent = [f"{case} ({a} x {b})" for name, a, b, case in fixtures
              if (name == cohort or a in ids or b in ids) and not (a in ids and b in ids)]
    if absent:
        raise ValueError(f"keep-apart fixtures absent from cohort {cohort}: {absent}; re-read them against "
                         "this export and edit autodedup/lab/keep_apart.json")
    member_of = groups.member_of
    present = [(a, b, case) for _, a, b, case in fixtures if a in ids and b in ids]
    joined = [(a, b, case) for a, b, case in present if a in member_of and member_of[a] == member_of.get(b)]
    cases = {case for *_, case in present}
    return {"n": len(present), "apart": len(present) - len(joined), "cases": len(cases),
            "cases_apart": len(cases - {case for *_, case in joined}),
            "together": [f"{case} ({a} x {b})" for a, b, case in joined]}


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


def _reads(sample: list[frozenset[int]], reads: dict[frozenset[int], str]) -> dict[str, Any]:
    """M4 / M5: the seeded sample's group reads; `fused` is a `different` read (the strict
    classification, undecidable counted as fused, is the reader's), with its Wilson upper bound."""
    same = sum(1 for g in sample if reads.get(g) == SAME)
    fused = sum(1 for g in sample if reads.get(g) == DIFFERENT)
    read = same + fused
    return {"sampled": len(sample), "read": read, "fused": fused, "one_property": same,
            "fused_upper": round(wilson_interval(fused, read)[1], 4) if read else None}


def _splits(arm_sets: set[frozenset[int]], base_sets: set[frozenset[int]]) -> set[frozenset[int]]:
    """The base groups the arm breaks up (a base group wholly inside an arm group was absorbed)."""
    formed = arm_sets - base_sets
    return {g for g in base_sets - arm_sets if not any(g <= f for f in formed)}


def auc(score: np.ndarray, positive: np.ndarray) -> float | None:
    """Mann-Whitney AUC, ties shared."""
    n_pos, n_neg = int(positive.sum()), int((~positive).sum())
    if not n_pos or not n_neg:
        return None
    order = np.argsort(score, kind="stable")
    ranks = np.empty(len(score))
    sorted_scores = score[order]
    i = 0
    while i < len(score):
        j = i
        while j + 1 < len(score) and sorted_scores[j + 1] == sorted_scores[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return round(float((ranks[positive].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)), 4)


def m6(c: Cohort, d: Any, labels: dict[tuple[int, int], str], min_negatives: int = 30
       ) -> dict[str, Any]:
    """M6, DESCRIPTIVE ONLY (D11): the arm's score column on the cohort's operator-ruled candidate
    pairs, overall and per scope with enough negatives. Sealed by town only when the arm's score
    file was fitted so; the lab cannot tell."""
    index = {key: i for i, key in enumerate(c.keys)}
    rows = [(index[k], v == SAME) for k, v in labels.items() if k in index]
    if not rows:
        return {"n": 0, "auc": None, "scopes": {}}
    idx = np.array([i for i, _ in rows])
    pos = np.array([p for _, p in rows])
    score = d.score[idx]
    out: dict[str, Any] = {"n": len(rows), "auc": auc(score, pos), "scopes": {}}
    scopes = np.array([f"{c.ds.listings[c.keys[i][0]].category_type}|"
                       f"{c.ds.listings[c.keys[i][0]].category_main}" for i in idx], dtype=object)
    for scope in sorted(set(scopes)):
        hit = scopes == scope
        if int((~pos[hit]).sum()) >= min_negatives:
            out["scopes"][scope] = {"n": int(hit.sum()), "neg": int((~pos[hit]).sum()),
                                    "auc": auc(score[hit], pos[hit])}
    return out


def _run_seconds(c: Cohort) -> float | None:
    if c.run is None:
        return None
    try:
        timings = json.loads((c.run.dir / "run.json").read_text(encoding="utf-8"))["timings"]
    except (OSError, ValueError, KeyError):
        return None
    return round(float(timings.get("decide_s", 0.0)), 1)


def row(c: Cohort, arm: Outcome, base: Outcome | None, labels: Labels,
        why: dict[str, Any] | None = None, verified: bool | None = None, stamp: str = ""
        ) -> dict[str, Any]:
    d, g = arm.decisions, arm.groups
    ids = set(c.ds.listings)
    merges = d.zone == MERGE
    co = g.co_pairs()
    rulings = _read(g, labels.rulings, ids)
    fixtures = fixtures_apart(g, load_fixtures(), c.name, ids)
    sizes = [len(v) for v in g.clusters.values()]
    out: dict[str, Any] = {
        "experiment": arm.config.get("name"), "config_id": config_id(arm.config),
        "cohort": c.name, "cache": c.version, "stamp": stamp, "engine": c.code_digest,
        "code": git_head(), "verified": verified, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "zones": {"merge": int(merges.sum()), "band": int((d.zone == BAND).sum()),
                  "reject": int((d.zone == REJECT).sum()), "veto": int((d.zone == VETO).sum())},
        "merge_by": _count(f"{r}:{n}" if r in ("proof", "context") else r
                           for r, n in zip(d.rung[merges], d.name[merges])),
        "merge_carrier": _count(d.carrier[merges]),
        "groups": len(g.clusters), "grouped": sum(sizes), "copairs": len(co),
        "rulings": rulings, "fixtures": fixtures,
        "judges": {name: _read(g, pairs, ids) for name, pairs in labels.judges.items()},
        "timings": {k: round(v, 2) for k, v in arm.timings.items()},
        "walls_forced": arm.walls_forced,
    }
    lost = where_lost(c, arm, labels).get(f"operator:{SAME}", {})
    m: dict[str, Any] = {
        "M1": {"together": rulings["same_together"], "n": rulings["same_n"]},
        "M2": {"together": rulings["diff_together"], "n": rulings["diff_n"]},
        "M3": {k: fixtures[k] for k in ("apart", "n", "cases_apart", "cases", "together")},
        "M6": m6(c, d, labels.rulings),
        "M7": {"band": out["zones"]["band"], "groups": len(g.clusters),
               "largest": max(sizes) if sizes else 0},
        "M8": {"lost_same": {k: v for k, v in lost.items() if k != "together"},
               "why": why},
        "M9": {"decide_s": out["timings"].get("decide_s"), "group_s": out["timings"].get("group_s"),
               "harness_run_s": _run_seconds(c)},
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
        splits = _splits(arm_sets, base_sets)
        out["vs_base"] = {"base": base.config.get("name"), "zone_moves": zone_moves,
                          "copairs_gained": len(co - base_co), "copairs_lost": len(base_co - co),
                          "groups_arm_only": len(arm_sets - base_sets),
                          "groups_base_only": len(base_sets - arm_sets),
                          "adverts_moved": len(moved)}
        m["M4"] = _reads(draw(arm_sets - base_sets, M45_N, M45_SEED), labels.groups)
        m["M5"] = _reads(draw(splits, M45_N, M45_SEED), labels.groups)
        m["M7"].update({"copairs_gained": len(co - base_co), "copairs_lost": len(base_co - co),
                        "groups_arm_only": len(arm_sets - base_sets),
                        "groups_base_split": len(splits)})
        if labels.groups:
            out["group_reads"] = {
                side: {v: sum(1 for x in sets if labels.groups.get(x) == v)
                       for v in (SAME, DIFFERENT)}
                for side, sets in (("arm_only", arm_sets - base_sets),
                                   ("base_only", base_sets - arm_sets))}
    out["m"] = m
    return out


def why_summary(report: dict[str, dict[str, Any]], top: int = 3) -> dict[str, Any]:
    """M8's `--why` in one cell: refused merge edges and the facts that separate them, plus the
    operator `same` pairs among them."""
    edges = report.get("all_refused_edges", {})
    same = report.get(f"operator:{SAME}", {})
    return {"refused_edges": edges.get("n", 0),
            "facts": dict(list(edges.get("separating_facts", {}).items())[:top]),
            "operator_same_refused": same.get("n", 0),
            "operator_same_facts": dict(list(same.get("separating_facts", {}).items())[:top])}


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


def latest_rows(board: Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    """The newest row per (cohort, experiment, config)."""
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    if not board.is_file():
        return latest
    for line in board.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            latest[(entry["cohort"], entry["experiment"], entry["config_id"])] = entry
    return latest


def _read_cell(r: dict[str, Any] | None) -> str | None:
    if not r:
        return None
    if not r["read"]:
        return f"unread (0/{r['sampled']})"
    ub = f" ≤{100 * r['fused_upper']:.1f}%" if r.get("fused_upper") is not None else ""
    return f"{r['fused']} fused/{r['read']} read of {r['sampled']}{ub}"


def _m3_cell(fx: dict[str, Any] | None) -> str | None:
    if not fx:
        return None
    cell = f"{fx['apart']}/{fx['n']}"
    if "cases" in fx:
        cell += f" ({fx['cases_apart']}/{fx['cases']} cases)"
    return cell


def _m6_cell(m6_: dict[str, Any] | None) -> str | None:
    if not m6_ or m6_.get("auc") is None:
        return None
    scopes = ", ".join(f"{k} {v['auc']}" for k, v in m6_.get("scopes", {}).items())
    return f"{m6_['auc']} (n {m6_['n']}{'; ' + scopes if scopes else ''})"


def _m7_cell(m7: dict[str, Any], vs: dict[str, Any]) -> str:
    cell = f"band {m7['band']}, groups {m7['groups']}, max {m7['largest']}"
    if "copairs_gained" in m7:
        cell += (f"; co +{m7['copairs_gained']}/−{m7['copairs_lost']}, "
                 f"groups +{m7['groups_arm_only']}/−{m7['groups_base_split']}")
    return cell


def _m8_cell(m8: dict[str, Any]) -> str:
    lost = ", ".join(f"{k} {v}" for k, v in list(m8.get("lost_same", {}).items())[:3]) or "none"
    why = m8.get("why")
    if why:
        facts = ", ".join(f"{k} {v}" for k, v in why["facts"].items()) or "–"
        lost += (f"; why: {why['refused_edges']} refused edges ({facts}), "
                 f"op same {why['operator_same_refused']}")
    return lost


def render(board: Path, cohorts: Iterable[str] | None = None) -> str:
    """The one table (GLOBAL_SEARCH 2.2): one row per arm per cohort, M1-M9, newest row per
    (cohort, experiment, config), cohorts in the order given."""
    latest = latest_rows(board)
    wanted = list(cohorts) if cohorts else sorted({k[0] for k in latest})
    head = ("cohort", "arm", "verified", "M1 op same together", "M2 op different + MNL together",
            "M3 fixtures apart", "M4 arm-only read", "M5 base-only read", "M6 AUC (descriptive)",
            "M7 label-free", "M8 lost op same; why", "M9 decide s / group s (harness run s)")
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for cohort in wanted:
        rows = sorted((e for k, e in latest.items() if k[0] == cohort),
                      key=lambda e: (e["rulings"]["diff_together"], -e["rulings"]["same_together"],
                                     e["experiment"]))
        for e in rows:
            m = e.get("m") or {}
            m9 = m.get("M9") or {}
            m1, m2 = m.get("M1"), m.get("M2")
            lines.append("| " + " | ".join(_fmt(x).replace("|", "/") for x in (
                cohort, e["experiment"],
                {True: "yes", False: "NO"}.get(e.get("verified"), "–"),
                f"{m1['together']}/{m1['n']}" if m1 else None,
                f"{m2['together']}/{m2['n']}" if m2 else None,
                _m3_cell(e.get("fixtures")), _read_cell(m.get("M4")), _read_cell(m.get("M5")),
                _m6_cell(m.get("M6")),
                _m7_cell(m["M7"], e.get("vs_base") or {}) if m.get("M7") else None,
                _m8_cell(m["M8"]) if m.get("M8") else None,
                f"{m9.get('decide_s')} / {m9.get('group_s')} ({_fmt(m9.get('harness_run_s'))})"
                if m9 else None)) + " |")
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
