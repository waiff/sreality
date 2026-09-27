"""The D83 read of one operating point: seeded samples of the groups only the model-first engine
forms (screened by C7's fused-shape screen, and not), of the groups only the ladder forms (screened),
and of the labelled negative pairs only the model-first engine co-clusters; cards for each.

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.read_kit <run> <key> <cut> <cohort> <n_scr> <n_plain> <n_lad> <n_pairs>

Writes read_<tag>.json (the lists) and cards_<tag>.txt; the verdicts go in verdicts_<tag>.json by hand."""
from __future__ import annotations

import itertools
import json
import random
import sys

import numpy as np

from autodedup.body_align import aligned_difference
from autodedup.dataset import load

from autodedup_w15_g2.engine_g2 import EngineCfg, run_engine
from autodedup_w15_g2.facts7 import colive_days, facts, first_fact
from autodedup_w15_g2.labels_g2 import screen_pair
from autodedup_w15_g2.learn import load_cohort
from autodedup_w15_g2.metrics import copairs, group_sets, screened
from autodedup_w15_g2.paths import COHORTS, OUT
from autodedup_w15_g2.sweep import FACTS

SEED = 20260927


def advert_line(m: int, l, where) -> str:
    path_ = [int(x) for _, x in (l.price_history or []) if x][-4:]
    return (f"  {m} [{where.get(m, '-')}] {l.source} {l.category_type}/{l.category_main} {l.disposition} "
            f"{l.area_m2} fl{l.floor}/{l.total_floors} {l.price} {path_} {l.location.street_key} "
            f"{l.location.house_number or ''} {(l.first_seen_at or '')[:10]}..{(l.last_seen_at or '')[:10]} "
            f"brk {(l.broker_key or '')[:5]} | {' '.join((l.description or '').split())[:230]}")


def main(run: str, key: str, cut: float, name: str, n_scr: int, n_plain: int, n_lad: int, n_pairs: int) -> None:
    c = load_cohort(name)
    ds = load(str(COHORTS[name]))
    L = ds.listings
    desc = {lid: l.description for lid, l in L.items()}

    def unit_text(a: int, b: int) -> str | None:
        return "unit_text" if aligned_difference(desc[a], desc[b], 0.6, True) is not None else None

    p_all = np.load(OUT / f"cache/p_{run}_{name}_{key}.npy")
    p = p_all[: c.n_cand]
    pf = [first_fact(c.recs[a], c.recs[b], FACTS) for a, b in c.cand_keys]
    mf = run_engine(c.cand_keys, p, c.recs, EngineCfg(t_merge=cut, t_band=0.2, facts=FACTS, lazy_fact=unit_text),
                    pair_facts=pf)
    tag = f"r2_{name}_{key}_{cut}"
    (OUT / f"cache/groups_{tag}.json").write_text(json.dumps({str(k): v for k, v in mf.groups.items()}))
    lad = c.ladder["FULL"]["clusters"]
    where = {m: r for r, ms in lad.items() for m in ms}
    mine, theirs = group_sets(mf.groups), group_sets(lad)
    scr_m, scr_l = screened(mf.groups, c.recs), screened(lad, c.recs)
    only_m = sorted((sorted(g) for g in mine - theirs), key=lambda g: (-len(g), g))
    rng = random.Random(SEED)
    m_scr = [g for g in only_m if frozenset(g) in scr_m]
    m_plain = [g for g in only_m if frozenset(g) not in scr_m]
    l_scr = sorted((sorted(g) for g in scr_l - mine), key=lambda g: (-len(g), g))
    picks = {"mf_screened": rng.sample(m_scr, min(n_scr, len(m_scr))),
             "mf_plain": rng.sample(m_plain, min(n_plain, len(m_plain))),
             "ladder_screened": rng.sample(l_scr, min(n_lad, len(l_scr)))}
    pos = {k: i for i, k in enumerate(c.keys)}
    mcp, lcp = copairs(mf.groups), copairs(lad)
    negs = sorted(k for k, lab in c.labels.items() if lab.y == 0 and k in mcp and k not in lcp)
    picks["neg_pairs_mf_only"] = rng.sample(negs, min(n_pairs, len(negs)))
    counts = {"mf_only_groups": len(only_m), "mf_only_screened": len(m_scr), "mf_only_plain": len(m_plain),
              "ladder_only_screened": len(l_scr), "neg_pairs_mf_only": len(negs),
              "neg_pairs_mf_only_by_src": {}}
    for k in negs:
        s = c.labels[k].src
        counts["neg_pairs_mf_only_by_src"][s] = counts["neg_pairs_mf_only_by_src"].get(s, 0) + 1
    (OUT / f"read_{tag}.json").write_text(json.dumps({"counts": counts, "picks": picks}))
    out = [f"# {tag}: {json.dumps(counts)}"]
    for side in ("mf_screened", "mf_plain", "ladder_screened"):
        for gi, g in enumerate(picks[side], 1):
            parts: dict = {}
            src = lad if side != "ladder_screened" else mf.groups
            loc = where if side != "ladder_screened" else {m: r for r, ms in mf.groups.items() for m in ms}
            for m in g:
                parts.setdefault(loc.get(m), []).append(m)
            split = ", ".join(f"{'-' if k is None else len(src[k])}:{len(v)}" for k, v in parts.items())
            other = "ladder" if side != "ladder_screened" else "MF"
            out.append(f"\n## {side} {gi}: {len(g)} adverts; {other} split {split}")
            for m in g[:16]:
                out.append(advert_line(m, L[m], loc))
            if len(g) > 16:
                out.append(f"  ... {len(g) - 16} more")
            flagged = [(a, b) for a, b in itertools.combinations(sorted(g), 2) if screen_pair(c.recs[a], c.recs[b])]
            out.append(f"  flagged pairs {len(flagged)} of {len(g) * (len(g) - 1) // 2}")
            for a, b in flagged[:8]:
                ra, rb = c.recs[a], c.recs[b]
                i = pos.get((a, b))
                out.append(f"    {a}x{b} same_portal={ra.source == rb.source} colive={round(colive_days(ra, rb) or -1, 1)} "
                           f"p={round(float(p_all[i]), 3) if i is not None else None} "
                           f"prices={ra.prices[-3:]}/{rb.prices[-3:]}")
    for a, b in picks["neg_pairs_mf_only"]:
        lab = c.labels[(a, b)]
        i = pos.get((a, b))
        out.append(f"\n## pair {a} x {b}  label {lab.src} y={lab.y}  p={round(float(p_all[i]), 3) if i is not None else None}"
                   f"  facts={facts(c.recs[a], c.recs[b], FACTS)}  colive={colive_days(c.recs[a], c.recs[b])}")
        for m in (a, b):
            out.append(advert_line(m, L[m], where))
    (OUT / f"cards_{tag}.txt").write_text("\n".join(out) + "\n")
    print(json.dumps(counts))


if __name__ == "__main__":
    a = sys.argv[1:]
    main(a[0], a[1], float(a[2]), a[3], *(int(x) for x in a[4:8]))
