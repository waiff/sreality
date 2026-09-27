"""The model-first engine: seven typed facts forbid, one learned score proposes, one constrained
clustering groups. Nothing else decides.

  edge      = a retrieved candidate pair with no stated fact and calibrated p >= t_merge
  band      = no stated fact and t_band <= p < t_merge (the review queue; merges nothing)
  grouping  = edges in descending p; a union is refused when ANY cross pair of the two groups
              states a fact (the same seven comparators, read over every member pair, scored or
              not), or crosses an operator must-not-link; optional `t_neg`: also refused when a
              SCORED cross pair sits below t_neg (learned negative evidence, complete-link);
              must-links are contracted first and dissolve only on deal/kind (rule 15)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

from autodedup_w15_g2.facts7 import FactCfg, Rec, first_fact


@dataclass
class EngineCfg:
    t_merge: float
    t_band: float
    t_neg: float | None = None
    facts: FactCfg = field(default_factory=FactCfg)


@dataclass
class EngineRun:
    groups: dict[int, list[int]]
    edges: int
    band: int
    refused_fact: int
    refused_neg: int
    fact_pairs: dict[str, int]


def run_engine(keys: list[tuple[int, int]], p: np.ndarray, recs: dict[int, Rec], cfg: EngineCfg,
               must_link: Iterable[tuple[int, int]] = (), must_not: Iterable[tuple[int, int]] = (),
               pair_facts: list[str | None] | None = None) -> EngineRun:
    if pair_facts is None:
        pair_facts = [first_fact(recs[a], recs[b], cfg.facts) for a, b in keys]
    fact_count: dict[str, int] = {}
    for f in pair_facts:
        if f is not None:
            fact_count[f] = fact_count.get(f, 0) + 1
    pmap = {k: float(v) for k, v in zip(keys, p)}
    fact_memo: dict[tuple[int, int], str | None] = {k: f for k, f in zip(keys, pair_facts)}
    mnl = {tuple(sorted(k)) for k in must_not}

    parent: dict[int, int] = {}
    members: dict[int, list[int]] = {}

    def find(x: int) -> int:
        root = x
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(x, x) != root:
            parent[x], x = root, parent[x]
        return root

    def fact_of(x: int, y: int) -> str | None:
        k = (x, y) if x < y else (y, x)
        if k not in fact_memo:
            fact_memo[k] = first_fact(recs[k[0]], recs[k[1]], cfg.facts)
        return fact_memo[k]

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx == ry:
            return
        if len(members.get(rx, [rx])) < len(members.get(ry, [ry])):
            rx, ry = ry, rx
        parent[ry] = rx
        members[rx] = members.pop(rx, [rx]) + members.pop(ry, [ry])

    for a, b in must_link:
        if a in recs and b in recs and fact_of(a, b) not in ("deal", "kind"):
            union(a, b)

    order = [i for i in np.argsort(-p, kind="stable")
             if p[i] >= cfg.t_merge and pair_facts[i] is None]
    refused_fact = refused_neg = edges = 0
    for i in order:
        a, b = keys[i]
        ra, rb = find(a), find(b)
        if ra == rb:
            edges += 1
            continue
        ma, mb = members.get(ra, [ra]), members.get(rb, [rb])
        bad = False
        for x in ma:
            for y in mb:
                k = (x, y) if x < y else (y, x)
                if fact_of(x, y) is not None or k in mnl:
                    bad = True
                    refused_fact += 1
                    break
                if cfg.t_neg is not None:
                    q = pmap.get(k)
                    if q is not None and q < cfg.t_neg:
                        bad = True
                        refused_neg += 1
                        break
            if bad:
                break
        if bad:
            continue
        union(a, b)
        edges += 1
    groups = {r: sorted(m) for r, m in members.items() if len(m) > 1}
    band = int(sum(1 for i in range(len(keys))
                   if pair_facts[i] is None and cfg.t_band <= p[i] < cfg.t_merge))
    return EngineRun(groups, edges, band, refused_fact, refused_neg, fact_count)
