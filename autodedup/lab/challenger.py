"""The challenger on the plug board (GLOBAL_SEARCH 3.2, B-b): the rungs `mf_score` and `facts` and
the group step `mf_union`, each a thin call into `autodedup/challenger/`, so the lab's numbers come
from the code R2 would ship and from no second implementation.

    {"model": {"const": 0},
     "ladder": [{"rung": "veto"},
                {"rung": "mf_score", "model": "$AUTODEDUP_LAB_MODELS/all/{cohort}/{block}.json",
                 "t_merge": 0.8, "t_band": 0.2},
                {"rung": "facts"}],
     "group": {"step": "mf_union", "t_neg": 0.2}}

`mf_score` reads one model file per pair: `{block}` is the pair's lo advert's town, so a model
fitted sealed by town scores only its own town's pairs. The p column is computed once per set of
model files and cohort cache (pure Python, forked) and kept beside the overlay. `facts` reads the
pairs an arm left in the merge or band zone (place it after the score), and `mf_union` reads every
cross pair with the same fact function; their answers are kept in the overlay, keyed by the fact
module's code and the dials."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

from autodedup.challenger import facts as mf_facts
from autodedup.challenger.score import scorer
from autodedup.challenger.union import constrained_union
from autodedup.features import FEATURE_ORDER
from autodedup.lab.board import (BAND, FACT_FAMILY, MERGE, REJECT, U, VETO, Decisions, Groups, _cat,
                                 group_step, rung)
from autodedup.lab.cache import Cohort

CHALLENGER_DIR: Path = Path(mf_facts.__file__).resolve().parent
TYPED_FAMILY: dict[str, str] = {"deal": "ATTR", "kind": "ATTR", "area": "ATTR", "disposition": "ATTR",
                                "floor": "ATTR", "price": "PRICE", "unit": "TXT",
                                "body_align": "TXT"}
P_COLUMN: str = "mf_p"


def _file_sha(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()[:12]


# --- mf_score: the model files, one per town ------------------------------------------------------

def model_paths(c: Cohort, template: str) -> dict[str, Path]:
    """The model file of every town a pair's lo advert sits in (`{block}`), `{cohort}` and `$VARS`
    expanded; a town with no file is refused, never scored by another town's model."""
    towns = sorted({c.ds.listings[lo].block for lo, _ in c.keys})
    base = os.path.expandvars(template)
    out = {town: Path(base.format(cohort=c.name, block=town)) for town in towns}
    missing = sorted(town for town, path in out.items() if not path.is_file())
    if missing:
        raise FileNotFoundError(f"{c.name}: no model file for towns {missing} ({template})")
    return out


_SCORE_JOB: dict[str, Any] = {}


def _score_chunk(idx: list[int]) -> list[float]:
    job = _SCORE_JOB
    V, P, town, fns, width = job["V"], job["P"], job["town"], job["fns"], job["width"]
    out = []
    for i in idx:
        x = [float(v) if present else None for v, present in zip(V[i].tolist(), P[i].tolist())]
        x += [None] * (width - len(x))
        out.append(fns[town[i]](x))
    return out


def learned_scores(c: Cohort, template: str) -> tuple[np.ndarray, dict[str, Any]]:
    """The challenger's p for every pair and its provenance (each town's model file and card)."""
    paths = model_paths(c, template)
    specs = {town: json.loads(path.read_text("utf-8")) for town, path in paths.items()}
    for town, spec in specs.items():
        if list(spec["feature_order"][:len(FEATURE_ORDER)]) != list(FEATURE_ORDER):
            raise ValueError(f"{paths[town]}: its feature order is not this checkout's")
    shas = {town: _file_sha(path) for town, path in paths.items()}
    digest = hashlib.sha1(json.dumps([c.version, shas], sort_keys=True).encode()).hexdigest()[:12]
    provenance = {"kind": "mf", "template": template, "digest": digest, "files": shas,
                  "cards": {town: spec.get("card", {}) for town, spec in specs.items()}}
    memo = c.path / "mf_scores" / f"{digest}.npy"
    if memo.is_file():
        return np.load(memo), provenance
    names = sorted(specs)
    town_of = {town: k for k, town in enumerate(names)}
    _SCORE_JOB.update(V=c.V, P=c.P, fns=[scorer(specs[t]) for t in names],
                      town=np.array([town_of[c.ds.listings[lo].block] for lo, _ in c.keys]),
                      width=max(len(spec["feature_order"]) for spec in specs.values()))
    chunks = [list(range(k, min(k + 2000, c.n))) for k in range(0, c.n, 2000)]
    try:
        if c.workers <= 1 or len(chunks) <= 1:
            parts = [_score_chunk(chunk) for chunk in chunks]
        else:
            with get_context("fork").Pool(c.workers) as pool:
                parts = pool.map(_score_chunk, chunks, chunksize=1)
    finally:
        _SCORE_JOB.clear()
    column = np.array([p for part in parts for p in part], dtype=np.float64)
    memo.parent.mkdir(parents=True, exist_ok=True)
    tmp = memo.with_suffix(".tmp.npy")
    np.save(tmp, column)
    tmp.replace(memo)
    return column, provenance


@rung("mf_score")
def mf_score_rung(c: Cohort, d: Decisions, p: dict[str, Any]) -> Decisions:
    """The learned score decides: p at or above `t_merge` merges, at or above `t_band` bands, the
    rest is rejected. Every pair carries its p (the union's learned negatives read it)."""
    d = d.copy()
    column, provenance = learned_scores(c, p["model"])
    t_merge, t_band = float(p.get("t_merge", 0.8)), float(p.get("t_band", 0.2))
    d.score[:] = column
    d.columns[P_COLUMN] = column
    d.provenance = {**provenance, "t_merge": t_merge, "t_band": t_band}
    m = d.zone == U
    d.settle(m & (column >= t_merge), MERGE, "mf_score", "cut", "NONE", "mf_score")
    d.settle(m & (column >= t_band) & (column < t_merge), BAND, "mf_score", "band", "NONE",
             "mf_score")
    d.settle(m & (column < t_band), REJECT, "mf_score", "cut", "NONE", "mf_score")
    return d


# --- facts: one fact function, memoised in the overlay ------------------------------------------

def facts_code() -> str:
    """The challenger's fact code (a memo written by another version is never read)."""
    return hashlib.sha1(b"".join(_file_sha(path).encode() for path in
                                 sorted(CHALLENGER_DIR.glob("*.py")))).hexdigest()[:12]


def stated(c: Cohort, overrides: Mapping[str, Any] | None = None
           ) -> tuple[Callable[[int, int], str | None], dict[tuple[int, int], str | None]]:
    """`stated(lo, hi)` over the cohort, memoised under the fact code and the dials: a candidate
    pair is read with its own feature row, any other pair with none."""
    dials = dataclasses.replace(mf_facts.Dials(), **dict(overrides or {}))
    tag = json.dumps({"code": facts_code(), "dials": dataclasses.asdict(dials)}, sort_keys=True)
    memo = c.facts.setdefault(tag, {})
    fact = mf_facts.stated_difference(c.ds.listings, c.settings, dials)
    index = c.key_index()

    def read(lo: int, hi: int) -> str | None:
        key = (lo, hi)
        if key not in memo:
            i = index.get(key)
            memo[key] = fact(lo, hi, c.feats(i) if i is not None else None)
            c.dirty = True
        return memo[key]

    return read, memo


_FACT_JOB: dict[str, Any] = {}


def _fact_chunk(keys: list[tuple[int, int]]) -> list[str | None]:
    read = _FACT_JOB["read"]
    return [read(lo, hi) for lo, hi in keys]


def read_pairs(c: Cohort, read: Callable[[int, int], str | None],
               memo: dict[tuple[int, int], str | None], keys: list[tuple[int, int]]) -> None:
    """Fill the memo for `keys`, forked across the cohort's workers when there is enough to share."""
    todo = [k for k in keys if k not in memo]
    chunks = [todo[k:k + 500] for k in range(0, len(todo), 500)]
    if c.workers <= 1 or len(chunks) <= 1:
        for lo, hi in todo:
            read(lo, hi)
        return
    _FACT_JOB["read"] = read
    try:
        with get_context("fork").Pool(c.workers) as pool:
            parts = pool.map(_fact_chunk, chunks, chunksize=1)
    finally:
        _FACT_JOB.clear()
    for chunk, part in zip(chunks, parts):
        memo.update(zip(chunk, part))
    c.dirty = True


@rung("facts")
def facts_rung(c: Cohort, d: Decisions, p: dict[str, Any]) -> Decisions:
    """A stated difference (`challenger.facts`) vetoes every pair not yet settled below the band:
    it never merges and no group joins across it. `dials` overrides the fact dials."""
    d = d.copy()
    read, memo = stated(c, p.get("dials"))
    idx = np.flatnonzero(np.isin(d.zone, (U, MERGE, BAND)) & ~d.final)
    read_pairs(c, read, memo, [c.keys[i] for i in idx])
    names = np.empty(c.n, dtype=object)
    names[:] = ""
    for i in idx:
        names[i] = memo[c.keys[i]] or ""
    hit = names != ""
    families = np.array([TYPED_FAMILY.get(x) or FACT_FAMILY.get(x, "ATTR") for x in names],
                        dtype=object)
    d.settle(hit, VETO, "fact", names, families, _cat(["fact:", names]))
    d.final |= hit
    return d


# --- mf_union: the constrained union -----------------------------------------------------------

@group_step("mf_union")
def mf_union_group(c: Cohort, d: Decisions, p: dict[str, Any]) -> Groups:
    """The merge edges joined by `challenger.union` (`t_neg`, `must_link`, `must_not_link`), every
    cross pair read by the facts rung's own function (`dials`)."""
    read, _ = stated(c, p.get("dials"))
    learned = d.columns.get(P_COLUMN, d.score)
    merges = np.flatnonzero(d.zone == MERGE)
    edges = [(c.keys[i][0], c.keys[i][1], float(learned[i])) for i in merges]
    scored = dict(zip(c.keys, learned.tolist()))
    groups = constrained_union(edges, read, scored, float(p.get("t_neg", 0.2)),
                               [tuple(x) for x in p.get("must_link", ())],
                               [tuple(x) for x in p.get("must_not_link", ())])
    return Groups({g[0]: g for g in groups}, {"edges": len(edges)})
