"""Compact evidence cards for a read list: each group's members (with the ladder group each one sits
in), and only the member pairs C7's screen flags (co-live on one portal, or prices no path joins).

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.cards2 <cohort> <read list json> [--other <tag>]
"""
from __future__ import annotations

import itertools
import json
import sys

from autodedup.dataset import load

from autodedup_w15_g2.facts7 import colive_days, facts
from autodedup_w15_g2.labels_g2 import screen_pair
from autodedup_w15_g2.learn import load_cohort
from autodedup_w15_g2.paths import COHORTS


def main(argv: list[str]) -> None:
    name, path = argv[0], argv[1]
    groups = json.load(open(path))
    c = load_cohort(name)
    where = {m: r for r, ms in c.ladder["FULL"]["clusters"].items() for m in ms}
    ds = load(str(COHORTS[name]))
    L = ds.listings
    for gi, g in enumerate(groups, 1):
        lad = {}
        for m in g:
            lad.setdefault(where.get(m), []).append(m)
        print(f"## {gi}: {len(g)} adverts; ladder split: "
              + ", ".join(f"{'-' if k is None else len(c.ladder['FULL']['clusters'][k])}:{len(v)}"
                          for k, v in lad.items()))
        for m in g:
            l = L[m]
            path_ = [int(p) for _, p in (l.price_history or []) if p][-3:]
            print(f"  {m} [{where.get(m, '-')}] {l.source} {l.category_type}/{l.category_main} "
                  f"{l.disposition} {l.area_m2} fl{l.floor}/{l.total_floors} {l.price} {path_} "
                  f"{l.location.street_key} {l.location.house_number or ''} "
                  f"{(l.first_seen_at or '')[:10]}..{(l.last_seen_at or '')[:10]} brk {(l.broker_key or '')[:5]} "
                  f"| {' '.join((l.description or '').split())[:150]}")
        flagged = [(a, b) for a, b in itertools.combinations(sorted(g), 2)
                   if screen_pair(c.recs[a], c.recs[b])]
        print(f"  flagged pairs {len(flagged)} of {len(g) * (len(g) - 1) // 2}")
        for a, b in flagged[:12]:
            ra, rb = c.recs[a], c.recs[b]
            print(f"    {a}x{b} same_portal={ra.source == rb.source} colive={round(colive_days(ra, rb) or -1, 1)} "
                  f"facts={facts(ra, rb)} prices={ra.prices[-2:]}/{rb.prices[-2:]}")


if __name__ == "__main__":
    main(sys.argv[1:])
