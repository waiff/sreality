"""The learned score as a column: the cache's exact scores, a logistic model file scored in one
numpy pass, or a per-pair score file (any learner, any machine) keyed on EXACTLY the cohort's pairs
and naming the cache it was scored on, plus the per-family log-odds split that names a score
decision's carrier (LADDER_SPEC section 4, E12). Every column comes with its provenance, which the
leaderboard row carries."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np

from autodedup.features import FAMILY_OF
from autodedup.lab.cache import FIDX, REPO, Cohort
from autodedup.model import CALIBRATION_TIE_BREAK, LogisticModel

FAMILIES: tuple[str, ...] = ("ATTR", "PRICE", "TXT", "BRK", "LOC", "IMG", "TIME")


def _z(model: LogisticModel, c: Cohort) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    for name in model.feature_order:
        j = FIDX[name]
        raw = np.where(c.P[:, j], c.V[:, j], 0.0)
        scale = model.scales.get(name, 1.0) or 1.0
        out[name] = (raw - model.means.get(name, 0.0)) / scale
    return out


def terms(model: LogisticModel, c: Cohort) -> dict[str, np.ndarray]:
    """Every log-odds term as a column: `v:` value, `p:` presence, `i:` interaction."""
    z = _z(model, c)
    out: dict[str, np.ndarray] = {}
    for name in model.feature_order:
        out[f"v:{name}"] = model.weights.get(name, 0.0) * z[name]
        out[f"p:{name}"] = model.presence_weights.get(name, 0.0) * c.P[:, FIDX[name]]
    for left, right, coef in model.interactions:
        out[f"i:{left}*{right}"] = coef * z.get(left, 0.0) * z.get(right, 0.0)
    return out


def calibrate(model: LogisticModel, p: np.ndarray) -> np.ndarray:
    knots = model.calibration
    if not knots:
        return p
    xs = np.array([k[0] for k in knots])
    ys = np.array([k[1] for k in knots])
    return np.interp(p, xs, ys) * (1.0 - CALIBRATION_TIE_BREAK) + CALIBRATION_TIE_BREAK * p


def logistic_scores(model: LogisticModel, c: Cohort) -> np.ndarray:
    total = np.full(c.n, model.intercept)
    for column in terms(model, c).values():
        total = total + column
    return calibrate(model, 1.0 / (1.0 + np.exp(-total)))


class ScorerMismatch(ValueError):
    """A score file that is not keyed on this cohort's pairs, or names another cache."""


def file_sha(path: str | Path) -> str:
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()[:12]


def npz_meta(data: Any) -> dict[str, Any]:
    return json.loads(str(data["meta"])) if "meta" in data.files else {}


def external_scores(path: str | Path, c: Cohort) -> tuple[np.ndarray, dict[str, Any]]:
    """An `.npz` with `lo`, `hi`, `p` and a `meta` JSON string: a learner's calibrated probability
    per candidate pair. It must carry exactly the cohort's pairs in the artefact's order and name
    the artefact version it scored (`meta.cache`): a file keyed on another retrieval, or scored
    off another cache, is refused, never silently filled."""
    data = np.load(path)
    lo, hi = data["lo"].astype(np.int64), data["hi"].astype(np.int64)
    if len(lo) != c.n or not (np.array_equal(lo, c.lo) and np.array_equal(hi, c.hi)):
        theirs = set(zip(lo.tolist(), hi.tolist()))
        ours = set(c.keys)
        raise ScorerMismatch(f"{path}: {len(theirs - ours)} pairs not on the one path, "
                             f"{len(ours - theirs)} one-path pairs missing, or another order; "
                             "refit it on this cache (`lab fit`)")
    meta = npz_meta(data)
    if meta.get("cache") != c.version:
        raise ScorerMismatch(f"{path}: scored cache {meta.get('cache')!r}, this cohort's is "
                             f"{c.version}; refit it on this cache (`lab fit`)")
    return (data["p"].astype(np.float64),
            {"kind": "npz", "path": str(path), "sha": file_sha(path), "cache": meta["cache"],
             "fit": meta.get("fit"), "train": meta.get("train")})


def load_model(ref: str | Path) -> LogisticModel:
    path = Path(ref)
    if not path.is_absolute():
        path = REPO / path
    return LogisticModel.from_json(json.loads(path.read_text(encoding="utf-8")))


def scores(spec: Any, c: Cohort) -> tuple[np.ndarray, LogisticModel | None, dict[str, Any]]:
    """The score column an arm decides on and its provenance: `ref` (the cache's exact
    `predict_proba`), a model file path, `{"npz": path}` (`{cohort}` is the cohort's name,
    `$VARS` expand), or `{"const": p}` (no learned score at all)."""
    if spec in (None, "ref"):
        return c.sig["score_ref"], c.model, {"kind": "ref"}
    if isinstance(spec, dict) and "npz" in spec:
        column, provenance = external_scores(
            os.path.expandvars(str(spec["npz"]).format(cohort=c.name)), c)
        return column, None, provenance
    if isinstance(spec, dict) and "const" in spec:
        return np.full(c.n, float(spec["const"])), None, {"kind": "const"}
    model = load_model(spec)
    return logistic_scores(model, c), model, {"kind": "model", "path": str(spec),
                                              "version": model.version}


def family_split(model: LogisticModel, c: Cohort) -> dict[str, np.ndarray]:
    """Log-odds per family over PRESENT features; an interaction is split half and half; an
    absent feature's terms are MISSING, never a carrier (E12)."""
    out = {family: np.zeros(c.n) for family in FAMILIES}
    for key, column in terms(model, c).items():
        kind, name = key.split(":", 1)
        parts = name.split("*") if kind == "i" else [name]
        for base in parts:
            family = FAMILY_OF.get(base)
            if family not in out:
                continue
            present = c.P[:, FIDX[base]]
            out[family] += np.where(present, column / len(parts), 0.0)
    return out
