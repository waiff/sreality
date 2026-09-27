"""Training sets, the sealed town split and the cross-fitted scores.

Sealing: a fold is a TOWN (the export block: an obec or a Praha quarter). A labelled pair trains
only when BOTH its adverts sit in training towns, and is scored only by a model that saw neither
of its towns. Cohorts share no advert and no block, so a cohort held out whole is sealed by town.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from autodedup_w15_g2.learn import Cohort, Model, design

KINDS = ("hgb", "mlp", "lr")


@dataclass
class Ground:
    c: Cohort
    V: np.ndarray
    P: np.ndarray
    idx: np.ndarray       # labelled row indices
    y: np.ndarray
    w: np.ndarray
    src: np.ndarray
    origin: np.ndarray
    blocks: list[tuple[str, str]]
    row_blocks: list[tuple[str, str]]


def ground(c: Cohort, mode: str = "none", keep=lambda lab: True) -> Ground:
    V, P = design(c, mode)
    pos = {k: i for i, k in enumerate(c.keys)}
    idx, y, w, src, origin, blocks = [], [], [], [], [], []
    for k, lab in c.labels.items():
        if k not in pos or not keep(lab):
            continue
        idx.append(pos[k]); y.append(lab.y); w.append(lab.w); src.append(lab.src)
        origin.append(lab.origin); blocks.append((c.recs[k[0]].block, c.recs[k[1]].block))
    row_blocks = [(c.recs[a].block, c.recs[b].block) for a, b in c.keys]
    return Ground(c, V, P, np.array(idx, dtype=int), np.array(y, dtype=int),
                  np.array(w, dtype=float), np.array(src), np.array(origin), blocks, row_blocks)


def fit_on(parts: list[tuple[Ground, np.ndarray]], kind: str) -> Model:
    V = np.vstack([g.V[g.idx[m]] for g, m in parts])
    P = np.vstack([g.P[g.idx[m]] for g, m in parts])
    y = np.concatenate([g.y[m] for g, m in parts])
    w = np.concatenate([g.w[m] for g, m in parts])
    return Model(kind).fit(V, P, y, w)


def everything(g: Ground) -> np.ndarray:
    return np.ones(len(g.idx), dtype=bool)


def crossfit(g: Ground, kind: str, extra: list[Ground] = ()) -> np.ndarray:
    """Leave-one-town-out raw scores for EVERY row of `g` (candidates and labelled extras); the
    grounds in `extra` (other cohorts, other towns by construction) train every fold."""
    towns = sorted({a for a, _ in g.row_blocks})
    raw = np.full(len(g.row_blocks), np.nan)
    base = [(e, everything(e)) for e in extra]
    for town in towns:
        train = np.array([town not in pair for pair in g.blocks])
        model = fit_on([(g, train)] + base, kind)
        score_rows = np.array([rb[0] == town for rb in g.row_blocks])
        raw[score_rows] = model.raw(g.V[score_rows], g.P[score_rows])
    return raw


def sealed_fit(train: list[Ground], kind: str) -> Model:
    return fit_on([(g, everything(g)) for g in train], kind)
