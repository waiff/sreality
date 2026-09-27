"""Every label the prototype may use, one row per pair, with its provenance.

Precedence (highest wins): operator > must-not-link > C7 hand read > gold > vision > text.
Weights: operator 1.0, C7 one-group 0.8, C7 fused-screen negative 0.5, gold 1.0 / 0.67, vision
0.6, text 0.3 (the label store's own TIER_WEIGHTS for the judge tiers). The judge tiers are
language-model LABELS for training; no model decides a live merge (D19)."""
from __future__ import annotations

import itertools
import json
from dataclasses import dataclass

from autodedup.labels import (label_pairs, load_all_judgements, load_operator_labels,
                              operator_label_pairs, pair_key)

from autodedup_w15_g2.facts7 import Rec, colive_days
from autodedup_w15_g2.paths import C7_JUDGEMENTS, JUDGE_RUNS, LABELS, W6_LABELFILES, W6_RUNS

RANK = {"op": 9, "mnl": 8, "c7": 7, "gold": 6, "vision": 5, "text": 4}


@dataclass(slots=True)
class Lab:
    y: int
    w: float
    src: str      # op_explicit / op_implied / op_browse_merge / mnl / c7_one / c7_fused / gold / vision / text
    origin: str   # operator / c7 / judge_trial / w6

    @property
    def rank(self) -> int:
        return RANK[self.src.split("_")[0]] if self.src.split("_")[0] in RANK else RANK[self.src]


def _put(out: dict, key, lab: Lab) -> None:
    old = out.get(key)
    if old is None or lab.rank > old.rank:
        out[key] = lab


def operator_eval_pairs(ids: set[int]) -> tuple[set, set]:
    """C7's definition (c7/scripts/cmp.py): same = pair rulings + every member pair of an operator
    merge; different = negative rulings + must-not-link; a pair in both counts as different."""
    pos, neg = set(), set()
    for line in open(LABELS / "operator_labels.jsonl"):
        r = json.loads(line)
        k = pair_key(r["listing_lo"], r["listing_hi"])
        if k[0] in ids and k[1] in ids:
            (pos if r["verdict"] == "same" else neg).add(k)
    for line in open(LABELS / "must_not_link.jsonl"):
        r = json.loads(line)
        k = pair_key(r["listing_lo"], r["listing_hi"])
        if k[0] in ids and k[1] in ids:
            neg.add(k)
    for line in open(LABELS / "operator_merges.jsonl"):
        r = json.loads(line)
        m = sorted({int(x["listing_id"]) for x in r["members"]} & ids)
        pos |= {(a, b) for a, b in itertools.combinations(m, 2)}
    return pos - neg, neg


def c7_groups(cohort: str) -> dict[tuple[int, ...], str]:
    path = C7_JUDGEMENTS / f"{cohort}.json"
    if not path.is_file():
        return {}
    out = {}
    for k, v in json.load(open(path)).items():
        if k.startswith("_") or not isinstance(v, list):
            continue
        try:
            out[tuple(sorted(int(x) for x in k.split(",")))] = v[0]
        except ValueError:
            continue
    return out


def screen_pair(a: Rec, b: Rec, price_tol: float = 0.005) -> bool:
    """C7's fused-shape screen on one pair: co-live >1 day on one portal, or prices no path joins."""
    if a.source == b.source and (colive_days(a, b) or 0.0) > 1.0:
        return True
    if a.prices and b.prices and not any(abs(x - y) / max(x, y) <= price_tol
                                         for x in a.prices for y in b.prices):
        return True
    return False


def all_labels(cohort: str, recs: dict[int, Rec]) -> dict[tuple[int, int], Lab]:
    ids = set(recs)
    out: dict[tuple[int, int], Lab] = {}
    inside = lambda k: k[0] in ids and k[1] in ids  # noqa: E731

    for origin, paths in (("w6", [W6_LABELFILES / f"{r}.judgements.jsonl" for r in W6_RUNS]),
                          ("judge_trial", list(JUDGE_RUNS.values()))):
        for k, lab in label_pairs(load_all_judgements(paths)).items():
            if lab.y is None or not inside(k):
                continue
            _put(out, k, Lab(int(lab.y), float(lab.weight), lab.tier, origin))
    for k, verdict in c7_groups(cohort).items():
        if verdict == "one":
            for p in itertools.combinations(k, 2):
                if inside(p):
                    _put(out, p, Lab(1, 0.8, "c7_one", "c7"))
        elif verdict == "fused":
            for p in itertools.combinations(k, 2):
                if inside(p) and screen_pair(recs[p[0]], recs[p[1]]):
                    _put(out, p, Lab(0, 0.5, "c7_fused", "c7"))
    for line in open(LABELS / "must_not_link.jsonl"):
        r = json.loads(line)
        k = pair_key(r["listing_lo"], r["listing_hi"])
        if inside(k):
            _put(out, k, Lab(0, 1.0, "mnl", "operator"))
    ops = operator_label_pairs(load_operator_labels(LABELS / "operator_labels.jsonl"))
    for k, lab in ops.items():
        if inside(k):
            _put(out, k, Lab(int(lab.y), 1.0, f"op_{lab.source}", "operator"))
    pos, neg = operator_eval_pairs(ids)
    for k in pos:
        if k not in out or out[k].origin != "operator":
            _put(out, k, Lab(1, 1.0, "op_merge_member", "operator"))
    return out
