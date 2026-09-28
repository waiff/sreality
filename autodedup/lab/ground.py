"""`lab ground`: one cohort's labelled pairs as the challenger's training rows, built in the lab (B-b).

The labels are the ones the G2 prototype trained on, read through the engine's own label store
(`autodedup.labels`) under G2's precedence and weights: operator > must-not-link > C7 hand read >
gold > vision > text (a pair keeps its first label unless a higher one arrives); operator 1.0, a C7
one-group read 0.8, a C7 fused read 0.5 on the member pairs its screen names (co-live over a day on
one portal, or price paths that never meet within 0.5 %), the judge tiers at the store's weights,
the judge files read origin by origin in the registry's order. Every member pair of an operator
merge is a positive unless an operator label or must-not-link holds the pair.

A pair's feature row is the one path's: a candidate's row is the cache's (`harness run --evidence`),
and a labelled pair retrieval never produced is featurised by the lane's own explicit-pair call
(`harness.decide_explicit`). A cohort with no cache (an origin-fixture cohort) has every labelled
pair featurised that way off its export. The file carries its digest, its settings row's and the
cache's, and `lab mf-fit` trains on no other file.

The registry's `train` block names the label files: `{"judges": {origin: [paths]}, "c7": dir}`;
the operator's files are the registry's `labels`."""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from autodedup import evidence, harness
from autodedup.dataset import Dataset, Listing, load
from autodedup.features import FEATURE_ORDER
from autodedup.incremental import Calibration
from autodedup.indistinguishable import _price_points, honest_overlap_days, price_paths_agree
from autodedup.labels import (label_pairs, load_all_judgements, load_operator_labels,
                              operator_label_pairs, pair_key)
from autodedup.model import LogisticModel
from autodedup.settings import Settings

BUILDER: str = "autodedup.lab.ground"
RANK: dict[str, int] = {"op": 9, "mnl": 8, "c7": 7, "gold": 6, "vision": 5, "text": 4}
C7_ONE, C7_FUSED = 0.8, 0.5
SCREEN_PRICE, SCREEN_COLIVE_DAYS = 0.005, 1.0


@dataclass(frozen=True)
class Lab:
    y: int
    w: float
    src: str
    origin: str

    @property
    def rank(self) -> int:
        return RANK.get(self.src.split("_")[0], RANK.get(self.src, 0))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def fused_screen(a: Listing, b: Listing) -> bool:
    """C7's fused-shape screen on one member pair: co-live over a day on one portal, or two price
    paths that never meet."""
    if a.source == b.source and (honest_overlap_days(a, b) or 0.0) > SCREEN_COLIVE_DAYS:
        return True
    return bool(_price_points(a) and _price_points(b)
                and not price_paths_agree(a, b, SCREEN_PRICE))


def _c7_reads(train: Mapping[str, Any], cohort: str) -> dict[tuple[int, ...], str]:
    """C7's group reads of the cohort: member set -> `one` / `fused` / ..."""
    path = Path(train["c7"]) / f"{cohort}.json" if train.get("c7") else None
    out: dict[tuple[int, ...], str] = {}
    for key, value in (json.loads(path.read_text(encoding="utf-8")) if path and path.is_file()
                       else {}).items():
        if key.startswith("_") or not isinstance(value, list):
            continue
        try:
            out[tuple(sorted(int(x) for x in key.split(",")))] = value[0]
        except ValueError:
            continue
    return out


def labels(reg: Mapping[str, Any], cohort: str, listings: Mapping[int, Listing]
           ) -> dict[tuple[int, int], Lab]:
    """Every label on a pair of `listings`, in first-arrival order, the highest rank kept."""
    ids = set(listings)
    out: dict[tuple[int, int], Lab] = {}

    def inside(k: tuple[int, int]) -> bool:
        return k[0] in ids and k[1] in ids

    def put(k: tuple[int, int], lab: Lab) -> None:
        if k not in out or lab.rank > out[k].rank:
            out[k] = lab

    train = reg.get("train") or {}
    for origin, paths in (train.get("judges") or {}).items():
        for k, lab in label_pairs(load_all_judgements(paths)).items():
            if lab.y is not None and inside(k):
                put(k, Lab(int(lab.y), float(lab.weight), lab.tier, origin))
    for members, verdict in _c7_reads(train, cohort).items():
        for k in itertools.combinations(members, 2):
            if not inside(k):
                continue
            if verdict == "one":
                put(k, Lab(1, C7_ONE, "c7_one", "c7"))
            elif verdict == "fused" and fused_screen(listings[k[0]], listings[k[1]]):
                put(k, Lab(0, C7_FUSED, "c7_fused", "c7"))
    root = Path(reg["labels"])
    for row in _jsonl(root / "must_not_link.jsonl"):
        k = pair_key(row["listing_lo"], row["listing_hi"])
        if inside(k):
            put(k, Lab(0, 1.0, "mnl", "operator"))
    for k, lab in operator_label_pairs(load_operator_labels(root / "operator_labels.jsonl")).items():
        if inside(k):
            put(k, Lab(int(lab.y), 1.0, f"op_{lab.source}", "operator"))
    same: set[tuple[int, int]] = set()
    apart: set[tuple[int, int]] = set()
    for row in _jsonl(root / "operator_labels.jsonl"):
        k = pair_key(row["listing_lo"], row["listing_hi"])
        if inside(k):
            (same if row["verdict"] == "same" else apart).add(k)
    for row in _jsonl(root / "must_not_link.jsonl"):
        k = pair_key(row["listing_lo"], row["listing_hi"])
        if inside(k):
            apart.add(k)
    for row in _jsonl(root / "operator_merges.jsonl"):
        members = sorted({int(x["listing_id"]) for x in row["members"]} & ids)
        same |= {(a, b) for a, b in itertools.combinations(members, 2)}
    for k in same - apart:
        if k not in out or out[k].origin != "operator":
            put(k, Lab(1, 1.0, "op_merge_member", "operator"))
    return out


def _explicit(ds: Dataset, settings: Settings, model: LogisticModel,
              fps: Mapping[int, Any] | None, pairs: list[tuple[int, int]]) -> dict[Any, Any]:
    """The lane's explicit-pair features (`harness.decide_explicit`), per pair."""
    if not pairs:
        return {}
    cohort = None if fps is None else (dict(fps), Calibration.build(dict(fps), ds.listings,
                                                                    settings))
    return {k: row["feats"] for k, row in
            harness.decide_explicit(ds, settings, model, pairs, cohort).items()}


def digest(arrays: Mapping[str, np.ndarray]) -> str:
    """The ground's rows: keys, features, labels, weights, origins and towns."""
    body = hashlib.sha1()
    for name in ("keys", "V", "P", "y", "w", "origin", "src", "blocks", "feature_order"):
        body.update(name.encode() + b"\0" + np.ascontiguousarray(arrays[name]).tobytes() + b"\0")
    return body.hexdigest()[:12]


def build(reg: Mapping[str, Any], name: str, cache: Any | None = None,
          settings_name: str = "w31", model_name: str = "w6_gold") -> dict[str, Any]:
    """The ground of cohort `name` as the arrays `lab mf-fit` reads, plus its `meta`. `cache` is
    the cohort's lab cache (`open_cohort`), or None for a cohort the lab holds only as an export."""
    spec = reg["cohorts"][name]
    if cache is None:
        evidence.check_seal(spec["export"], reg.get("seals") or (), freeze=bool(spec.get("freeze")))
        ds, settings, model = load(spec["export"]), harness.named_settings(settings_name), \
            harness.named_model(model_name)
        fps, index, version = None, {}, None
    else:
        ds, settings, model, fps = cache.ds, cache.settings, cache.model, cache.fps
        index, version = {k: i for i, k in enumerate(cache.keys)}, cache.version
    found = labels(reg, name, ds.listings)
    keys = list(found)
    extra = [k for k in keys if k not in index]
    explicit = _explicit(ds, settings, model, fps, extra)
    V = np.zeros((len(keys), len(FEATURE_ORDER)))
    P = np.zeros((len(keys), len(FEATURE_ORDER)), dtype=bool)
    for r, k in enumerate(keys):
        if k in index:
            V[r], P[r] = cache.V[index[k]], cache.P[index[k]]
            continue
        for j, feature in enumerate(FEATURE_ORDER):
            V[r, j], P[r, j] = explicit[k].get(feature, (0.0, False))
    L = ds.listings
    arrays = {"keys": np.array(keys, dtype=np.int64).reshape(-1, 2), "V": V, "P": P,
              "y": np.array([found[k].y for k in keys], dtype=np.int64),
              "w": np.array([found[k].w for k in keys], dtype=np.float64),
              "origin": np.array([found[k].origin for k in keys], dtype=str),
              "src": np.array([found[k].src for k in keys], dtype=str),
              "blocks": np.array([(L[a].block, L[b].block) for a, b in keys], dtype=str
                                 ).reshape(-1, 2),
              "towns": np.array(sorted({x.block for x in L.values()}), dtype=str),
              "feature_order": np.array(FEATURE_ORDER, dtype=str)}
    meta = {"builder": BUILDER, "cohort": name, "digest": digest(arrays), "cache": version,
            "export": spec["export"], "settings": hashlib.sha1(
                evidence.settings_bytes(settings)).hexdigest()[:12],
            "rows": len(keys), "candidates": len(keys) - len(extra), "featurised": len(extra),
            "label_files": _label_files(reg, name)}
    return {**arrays, "meta": np.array(json.dumps(meta, sort_keys=True))}


def _label_files(reg: Mapping[str, Any], cohort: str) -> dict[str, str]:
    train = reg.get("train") or {}
    paths = [Path(reg["labels"]) / f for f in ("operator_labels.jsonl", "must_not_link.jsonl",
                                               "operator_merges.jsonl")]
    paths += [Path(p) for files in (train.get("judges") or {}).values() for p in files]
    if train.get("c7"):
        paths.append(Path(train["c7"]) / f"{cohort}.json")
    return {str(p): (hashlib.sha1(p.read_bytes()).hexdigest()[:12] if p.is_file() else "absent")
            for p in paths}


def write(path: str | Path, ground: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.npz")
    np.savez_compressed(tmp, **ground)
    tmp.replace(path)
