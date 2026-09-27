"""G2 v2: every cohort scored SEALED BY TOWN, with every other town's labels training it.

    PYTHONPATH=<worktree> python3 -m autodedup_w15_g2.sealed <out tag> <cohort> [<cohort> ...]

A fold is one town (the export block). For each town T of each cohort, one model is fitted on every
labelled pair (all cohorts) that touches no advert of T, and scores every candidate row whose `lo`
advert sits in T. Cohorts share no advert and no town, so a cohort's own rows are scored only by
models that never saw its town. The isotonic map that turns raw scores into p is fitted, per
evaluated cohort, on the out-of-town labelled rows of the OTHER cohorts only.

w6_gold is scored as shipped (its own calibration, its own cuts). It is in-sample on the trial
(fitted on judge labels of the trial export) and on the c18 judge rows (origin 'w6')."""
from __future__ import annotations

import collections
import json
import sys
import time
from collections.abc import Container

import numpy as np

from autodedup_w15_g2 import fixtures
from autodedup_w15_g2.engine_g2 import EngineCfg, run_engine
from autodedup_w15_g2.facts7 import FACTS, FactCfg, first_fact
from autodedup_w15_g2.fit import Ground, ground
from autodedup_w15_g2.learn import W6, Model, auc, fit_isotonic, load_cohort, load_g1
from autodedup_w15_g2.metrics import copairs, measure
from autodedup_w15_g2.paths import OUT


SOURCES = {
    "all": lambda lab: True,
    "op_c7": lambda lab: lab.origin in ("operator", "c7"),
    "op": lambda lab: lab.origin == "operator",
    "judge": lambda lab: lab.origin in ("judge_trial", "w6"),
}


def score_file(key: str) -> str:
    return key.replace("[", "-").replace("]", "")


def towns(g: Ground) -> list[str]:
    return sorted({b for pair in g.row_blocks for b in pair} | {b for pair in g.blocks for b in pair})


def oof(grounds: dict[str, Ground], kind: str, keep=None, log: str = "") -> dict[str, np.ndarray]:
    """Leave-one-town-out raw scores for every row of every cohort (candidates and labelled extras)."""
    clock = time.perf_counter()
    masks = {n: (np.array([keep(lab) for lab in g.labs]) if keep else np.ones(len(g.idx), bool))
             for n, g in grounds.items()}
    out = {n: np.full(len(g.row_blocks), np.nan) for n, g in grounds.items()}
    fitted: dict[tuple, Model] = {}
    for n, g in grounds.items():
        lo_town = np.array([rb[0] for rb in g.row_blocks])
        for town in sorted(set(lo_town)):
            touch = masks[n] & np.array([town in pair for pair in g.blocks])
            key = (n, tuple(np.where(touch)[0])) if touch.any() else ()
            model = fitted.get(key)
            if model is None:
                parts = []
                for m, h in grounds.items():
                    use = masks[m] & ~touch if m == n else masks[m]
                    if use.any():
                        parts.append((h, use))
                V = np.vstack([h.V[h.idx[u]] for h, u in parts])
                P = np.vstack([h.P[h.idx[u]] for h, u in parts])
                y = np.concatenate([h.y[u] for h, u in parts])
                w = np.concatenate([h.w[u] for h, u in parts])
                model = fitted[key] = Model(kind).fit(V, P, y, w)
            rows = lo_town == town
            out[n][rows] = model.raw(g.V[rows], g.P[rows])
        print(f"{log} {kind} {n} oof done {time.perf_counter()-clock:.0f}s", flush=True)
    return out


POOLED = True


def calibrate(grounds: dict[str, Ground], raw: dict[str, np.ndarray], keep=None) -> dict:
    """p per cohort through ONE isotonic map fitted on every cohort's labelled OUT-OF-TOWN rows.

    The map sees the evaluated cohort's labels (on scores of models that never saw its towns);
    the ranking never does. `POOLED = False` fits the map on the OTHER cohorts only, which is
    sealed but unstable for the trial: the other cohorts hold 103 negatives against 3,300
    positives, and the trial's band then moved 1,867 -> 5,791 on a 34-label change (s4)."""
    out = {}
    for n in grounds:
        xs, ys, ws = [], [], []
        for m, h in grounds.items():
            if m == n and not POOLED:
                continue
            sel = np.array([keep(lab) for lab in h.labs]) if keep else np.ones(len(h.idx), bool)
            xs.append(raw[m][h.idx[sel]]); ys.append(h.y[sel]); ws.append(h.w[sel])
        iso = fit_isotonic(np.concatenate(xs), np.concatenate(ys), np.concatenate(ws))
        out[n] = iso.predict(raw[n])
    return out


def pair_aucs(g: Ground, p: np.ndarray, nofact: np.ndarray) -> dict:
    out = {}
    sealed_for_w6 = g.origin != "w6"
    for name, m in (("operator", g.origin == "operator"), ("c7", g.origin == "c7"),
                    ("judge", np.isin(g.origin, ["judge_trial", "w6"])),
                    ("all", np.ones(len(g.idx), bool)), ("not_w6_origin", sealed_for_w6)):
        for tag, mm in (("", m), ("_nofact", m & nofact[g.idx])):
            a = auc(p[g.idx[mm]], g.y[mm])
            out[name + tag] = {"auc": None if a is None else round(a, 4), "n": int(mm.sum()),
                               "neg": int((g.y[mm] == 0).sum())}
    return out


def adversary(name: str, groups: dict, ids: Container[int]) -> dict:
    """M3 on every keep-apart fixture whose two adverts are in `ids` (`fixtures.present` raises on one missing)."""
    where = {m: r for r, ms in groups.items() for m in ms}
    out = {}
    for label, (a, b) in fixtures.present(name, ids).items():
        ra, rb = where.get(a), where.get(b)
        together = ra is not None and ra == rb
        out[label] = {"together": together,
                      "group_size": len(groups[ra]) if together else None,
                      "a_group": len(groups[ra]) if ra is not None else 1,
                      "b_group": len(groups[rb]) if rb is not None else 1}
    return out


def main(tag: str, names: list[str]) -> None:
    clock = time.perf_counter()
    mode = next((a.split("=", 1)[1] for a in names if a.startswith("--mode=")), "none")
    # G1's image-retrieval side file per cohort (JSONL: lo, hi and the G1_SLOTS), "{cohort}" in path
    g1 = next((a.split("=", 1)[1] for a in names if a.startswith("--g1=")), None)
    quick = "--quick" in names
    names = [a for a in names if not a.startswith("--")]
    cs = {n: load_cohort(n) for n in names}
    grounds: dict[str, Ground] = {}
    for n, c in cs.items():
        g = ground(c, mode)
        if g1:
            G = load_g1(g1.format(cohort=n), c.keys)
            g.V = np.hstack([g.V, np.nan_to_num(G)])
            g.P = np.hstack([g.P, ~np.isnan(G)])
        pos = {k: i for i, k in enumerate(c.keys)}
        g.labs = [c.labels[c.keys[i]] for i in g.idx]
        grounds[n] = g
        del pos
    print("grounds", {n: (len(g.idx), int(g.y.sum()), int((g.y == 0).sum())) for n, g in grounds.items()},
          flush=True)
    facts_of = {n: [first_fact(c.recs[a], c.recs[b]) for a, b in c.keys] for n, c in cs.items()}
    nofact = {n: np.array([f is None for f in facts_of[n]]) for n in cs}
    fact_cfgs = {"f7": FactCfg(), "noprice": FactCfg(price_tol=10.0),
                 "f7s": FactCfg(colive_floor_delta=1, colive_price_tol=0.005)}
    cand_facts = {fv: {n: ([first_fact(c.recs[a], c.recs[b], cfg) for a, b in c.cand_keys]
                           if fv != "f7" else facts_of[n][: c.n_cand]) for n, c in cs.items()}
                  for fv, cfg in fact_cfgs.items()}

    scores: dict[str, dict[str, np.ndarray]] = {}
    w6 = W6()
    scores["w6_gold"] = {n: w6.score(c.V, c.P) for n, c in cs.items()}
    for kind in (("hgb",) if quick else ("hgb", "lr", "mlp")):
        raw = oof(grounds, kind, log=tag)
        for n, arr in raw.items():
            np.save(OUT / f"cache/raw_{tag}_{n}_{kind}.npy", arr)
        scores[kind] = calibrate(grounds, raw)
    for src in (() if quick else ("op_c7", "op", "judge")):
        raw = oof(grounds, "hgb", keep=SOURCES[src], log=f"{tag}:{src}")
        scores[f"hgb[{src}]"] = calibrate(grounds, raw, keep=SOURCES[src])

    configs = [  # (name, score key, t_merge, t_band, t_neg, facts variant)
        ("w6_gold", "w6_gold", 0.9788, 0.1823, None, "f7"),
        ("lr", "lr", 0.9, 0.2, None, "f7"),
        ("mlp", "mlp", 0.9, 0.2, None, "f7"),
        ("hgb", "hgb", 0.9, 0.2, None, "f7"),
        ("hgb@0.8", "hgb", 0.8, 0.2, None, "f7"),
        ("hgb@0.95", "hgb", 0.95, 0.2, None, "f7"),
        ("hgb@0.98", "hgb", 0.98, 0.2, None, "f7"),
        ("hgb+neg0.05", "hgb", 0.9, 0.2, 0.05, "f7"),
        ("hgb+neg0.2", "hgb", 0.9, 0.2, 0.2, "f7"),
        ("hgb-noprice", "hgb", 0.9, 0.2, None, "noprice"),
        ("w6_gold:f7s", "w6_gold", 0.9788, 0.1823, None, "f7s"),
        ("lr:f7s", "lr", 0.9, 0.2, None, "f7s"),
        ("mlp:f7s", "mlp", 0.9, 0.2, None, "f7s"),
        ("hgb:f7s", "hgb", 0.9, 0.2, None, "f7s"),
        ("hgb:f7s@0.8", "hgb", 0.8, 0.2, None, "f7s"),
        ("hgb:f7s@0.95", "hgb", 0.95, 0.2, None, "f7s"),
        ("hgb:f7s@0.98", "hgb", 0.98, 0.2, None, "f7s"),
        ("hgb[op_c7]", "hgb[op_c7]", 0.9, 0.2, None, "f7"),
        ("hgb[op]", "hgb[op]", 0.9, 0.2, None, "f7"),
        ("hgb[judge]", "hgb[judge]", 0.9, 0.2, None, "f7"),
    ]
    for key, per in scores.items():
        for n, arr in per.items():
            np.save(OUT / f"cache/p_{tag}_{n}_{score_file(key)}.npy", arr)
    configs = [cfg for cfg in configs if cfg[1] in scores]
    results = {"tag": tag, "mode": mode, "cohorts": {}, "configs": configs, "facts": list(FACTS),
               "fact_dials": FactCfg().dials()}
    for n, c in cs.items():
        g = grounds[n]
        ref = c.ladder["FULL"]["clusters"]
        pf = facts_of[n][: c.n_cand]
        res = {"adverts": len(c.recs), "candidates": c.n_cand, "labels": len(g.idx),
               "labels_pos": int(g.y.sum()), "labels_neg": int((g.y == 0).sum()),
               "labels_by_origin": dict(collections.Counter(map(str, g.origin))),
               "towns": len({r.block for r in c.recs.values()}),
               "facts_on_candidates": {f: int(sum(1 for x in pf if x == f)) for f in FACTS},
               "ladder": {}, "engine": {}}
        for arm in ("FULL", "FOLD7", "FOLD7_IMG"):
            if arm not in c.ladder:
                continue
            m = measure(n, c.ladder[arm]["clusters"], c.recs, c.labels, ref)
            zone = c.ladder[arm]["zone"]
            m.update({"band": int((zone == "band").sum()), "merge_pairs": int((zone == "merge").sum()),
                      "seconds": round(c.ladder[arm]["seconds"], 1),
                      "adversary": adversary(n, c.ladder[arm]["clusters"], c.recs),
                      "groups_ge10": sum(1 for v in c.ladder[arm]["clusters"].values() if len(v) >= 10)})
            res["ladder"][arm] = m
        for cname, key, t_merge, t_band, t_neg, fv in configs:
            p_all = scores[key][n]
            t = time.perf_counter()
            run = run_engine(c.cand_keys, p_all[: c.n_cand], c.recs,
                             EngineCfg(t_merge=t_merge, t_band=t_band, t_neg=t_neg,
                                       facts=fact_cfgs[fv]),
                             pair_facts=cand_facts[fv][n])
            m = measure(n, run.groups, c.recs, c.labels, ref)
            m.update({"band": run.band, "merge_edges": run.edges, "refused_fact": run.refused_fact,
                      "refused_neg": run.refused_neg, "seconds": round(time.perf_counter() - t, 2),
                      "adversary": adversary(n, run.groups, c.recs),
                      "groups_ge10": sum(1 for v in run.groups.values() if len(v) >= 10),
                      "pair_auc": pair_aucs(g, p_all, nofact[n])})
            res["engine"][cname] = m
            print(n, cname, {k: v for k, v in m.items()
                             if k in ("groups", "copairs", "op_same_together", "op_same", "op_diff_apart",
                                      "op_diff", "screen_flagged_groups", "screen_flagged_not_in_ladder",
                                      "band", "max_group")}, flush=True)
            if cname in ("hgb", "hgb:f7s"):
                np.save(OUT / f"cache/p_{tag}_{n}_hgb.npy", p_all)
                suffix = "hgb" if cname == "hgb" else "hgb_f7s"
                with open(OUT / f"cache/groups_{tag}_{n}_{suffix}.json", "w") as handle:
                    json.dump({str(k): v for k, v in run.groups.items()}, handle)
        results["cohorts"][n] = res
    path = OUT / f"sealed_{tag}.json"
    path.write_text(json.dumps(results, indent=1, default=str))
    print(f"wrote {path} in {time.perf_counter()-clock:.0f}s", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
