"""One metric set for both engines (the ladder's clusters and the model-first groups)."""
from __future__ import annotations

import itertools

from autodedup_w15_g2.facts7 import Rec
from autodedup_w15_g2.labels_g2 import c7_groups, operator_eval_pairs, screen_pair


def copairs(groups: dict) -> set[tuple[int, int]]:
    out = set()
    for members in groups.values():
        out |= set(itertools.combinations(sorted(members), 2))
    return out


def group_sets(groups: dict) -> set[frozenset]:
    return {frozenset(m) for m in groups.values() if len(m) > 1}


def screened(groups: dict, recs: dict[int, Rec]) -> set[frozenset]:
    """C7's fused-shape screen at group grain: any member pair co-live >1 day on one portal, or
    two prices no path joins (0.5 %). Blind to sequential siblings with an agreeing path."""
    out = set()
    for m in group_sets(groups):
        if any(screen_pair(recs[a], recs[b]) for a, b in itertools.combinations(sorted(m), 2)):
            out.add(m)
    return out


def measure(name: str, groups: dict, recs: dict[int, Rec], labels: dict,
            ref_groups: dict | None = None) -> dict:
    ids = set(recs)
    pos, neg = operator_eval_pairs(ids)
    cp = copairs(groups)
    gs = group_sets(groups)
    scr = screened(groups, recs)
    c7 = c7_groups(name)
    c7_pos = {p for k, v in c7.items() if v == "one" for p in itertools.combinations(k, 2)}
    c7_neg = {k for k, lab in labels.items() if lab.src == "c7_fused"}
    j_pos = {k for k, lab in labels.items() if lab.origin in ("judge_trial", "w6") and lab.y == 1}
    j_neg = {k for k, lab in labels.items() if lab.origin in ("judge_trial", "w6") and lab.y == 0}
    out = {
        "groups": len(gs),
        "adverts_grouped": sum(len(g) for g in gs),
        "copairs": len(cp),
        "max_group": max((len(g) for g in gs), default=0),
        "op_same_together": len(pos & cp), "op_same": len(pos),
        "op_diff_apart": len(neg - cp), "op_diff": len(neg),
        "op_diff_coclustered": sorted(neg & cp),
        "c7_one_pairs_together": len(c7_pos & cp), "c7_one_pairs": len(c7_pos),
        "c7_fused_pairs_apart": len(c7_neg - cp), "c7_fused_pairs": len(c7_neg),
        "judge_pos_together": len(j_pos & cp), "judge_pos": len(j_pos),
        "judge_neg_apart": len(j_neg - cp), "judge_neg": len(j_neg),
        "screen_flagged_groups": len(scr),
    }
    if ref_groups is not None:
        rs = group_sets(ref_groups)
        new = gs - rs
        out["groups_not_in_ladder"] = len(new)
        out["screen_flagged_not_in_ladder"] = len(scr - rs)
        out["ladder_groups_not_here"] = len(rs - gs)
        rcp = copairs(ref_groups)
        out["copairs_gained_vs_ladder"] = len(cp - rcp)
        out["copairs_lost_vs_ladder"] = len(rcp - cp)
        out["_new_flagged"] = [sorted(g) for g in sorted(scr - rs, key=lambda g: -len(g))]
    return out
