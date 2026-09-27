"""Evidence cards for groups (for the hand read of screened groups): columns, price paths, live
windows, the seven facts and the ladder's readers on every member pair, tight photo frames.

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.cards <cohort> <groups.json> [--sample N]
"""
from __future__ import annotations

import itertools
import json
import random
import sys

from autodedup import indistinguishable as ind
from autodedup.dataset import hamming64, load
from autodedup.settings import Settings

from autodedup_w15_g2.facts7 import facts, rec_of
from autodedup_w15_g2.ladder import SETTINGS, _ORIGINAL_DF
from autodedup_w15_g2.labels_g2 import screen_pair
from autodedup_w15_g2.paths import COHORTS


def tight(ds, lo: int, hi: int) -> int:
    A = [i.phash for i in ds.images(lo) if i.phash is not None and (i.pop or 0) < 8]
    B = [i.phash for i in ds.images(hi) if i.phash is not None and (i.pop or 0) < 8]
    used, n = set(), 0
    for a in A:
        for j, b in enumerate(B):
            if j not in used and hamming64(a, b) <= 6:
                used.add(j)
                n += 1
                break
    return n


def main(argv: list[str]) -> None:
    name, path = argv[0], argv[1]
    groups = json.load(open(path))
    if "--sample" in argv:
        n = int(argv[argv.index("--sample") + 1])
        if len(groups) > n:
            groups = random.Random(20260927).sample(groups, n)
    ds = load(str(COHORTS[name]))
    S = Settings.from_json(SETTINGS)
    L = ds.listings
    for gi, g in enumerate(groups, 1):
        print(f"## group {gi}: {len(g)} adverts {g}")
        for i in g:
            l = L[i]
            ph = [p for _, p in (l.price_history or [])][-4:]
            print(f"  {i} {l.source} {l.category_type}/{l.category_main} {l.disposition} {l.area_m2}m2 "
                  f"fl {l.floor}/{l.total_floors} price {l.price} path {ph} | {l.location.street_key} "
                  f"{l.location.house_number or ''} | {(l.first_seen_at or '')[:10]}..{(l.last_seen_at or '')[:10]} "
                  f"brk {(l.broker_key or '')[:6]} | {' '.join((l.description or '').split())[:160]}")
        recs = {i: rec_of(L[i], S) for i in g}
        for a, b in itertools.combinations(sorted(g), 2):
            f7 = facts(recs[a], recs[b])
            rd = sorted({f.name for f in _ORIGINAL_DF(L[a], L[b], None, S, ind.CLUSTER)})
            print(f"    {a}x{b}: facts7={f7} readers={rd} screen={screen_pair(recs[a], recs[b])} "
                  f"tight={tight(ds, a, b)}")


if __name__ == "__main__":
    main(sys.argv[1:])
