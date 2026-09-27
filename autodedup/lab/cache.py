"""One cohort as the lab holds it, READ from a `harness run --evidence` artefact (B-a).

The pairs, their features and every rung's per-pair signal are the ones the one path decided and
the engine's own functions computed (`autodedup/evidence.py`); the lab builds nothing by a second
retrieval path. It adds two things only, both with the same engine functions: lazy fills of the
gate and promotion signals (for pairs an arm reaches beyond the run's store floor, or under an
arm's settings overrides, kept per override), and the D43 relation memo. Both live in an overlay
beside the artefact's version and only ever grow.

An artefact whose code digest is not the checkout's is refused before its pickle is read: its
signals were computed by an engine that is no longer this one, and a fill would mix the two. An
export a preregistration seals is refused unless the registry names its freeze."""

from __future__ import annotations

import dataclasses
import json
import os
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from autodedup import evidence
from autodedup.dataset import Dataset, load
from autodedup.features import ABSENT, FEATURE_ORDER, Feats
from autodedup.fingerprint import Fingerprint
from autodedup.harness import fingerprints
from autodedup.hazard_context import ContextIndex
from autodedup.model import LogisticModel
from autodedup.settings import Settings

OVERLAY_SCHEMA: int = 1
ENGINE_DIR: Path = Path(__file__).resolve().parents[1]
REPO: Path = ENGINE_DIR.parent
FIDX: dict[str, int] = {name: i for i, name in enumerate(FEATURE_ORDER)}
LAZY_SIGNALS: dict[str, tuple[str, ...]] = evidence.LAZY_SIGNALS
ZONE_CODE: dict[str, int] = {"veto": 1, "reject": 2, "band": 3, "merge": 4}


class StaleEvidence(RuntimeError):
    """The artefact was written by other engine code (or off another export) than this one."""


def registry(path: str | Path | None = None) -> dict[str, Any]:
    """The cohort registry: per cohort its export and its `harness run --evidence` directory."""
    raw = path or os.environ.get("AUTODEDUP_LAB_COHORTS")
    if not raw:
        raise SystemExit("no cohort registry: pass --cohorts or set AUTODEDUP_LAB_COHORTS")
    return json.loads(Path(raw).read_text(encoding="utf-8"))


def feats_of(V: np.ndarray, P: np.ndarray, i: int) -> Feats:
    return {name: ((float(V[i, j]), True) if P[i, j] else ABSENT)
            for j, name in enumerate(FEATURE_ORDER)}


def _objects(values: Any) -> np.ndarray:
    """An object column, element by element: a slice assignment would let numpy read a column of
    equal-length tuples (the gate's facts) as a 2-D array."""
    out = np.empty(len(values), dtype=object)
    for i, value in enumerate(values):
        out[i] = value
    return out


@dataclass
class Run:
    """The one path's own decision for every pair, the reference `lab verify` reproduces."""

    zone: np.ndarray
    reason: np.ndarray
    score: np.ndarray
    certificate: np.ndarray
    stored: np.ndarray
    dir: Path


@dataclass
class Cohort:
    """A cohort as the lab holds it: the engine's own objects plus the artefact's columns."""

    name: str
    spec: dict[str, Any]
    ds: Dataset
    settings: Settings
    model: LogisticModel
    fps: dict[int, Fingerprint]
    keys: list[tuple[int, int]]
    probes: list[frozenset]
    V: np.ndarray
    P: np.ndarray
    sig: dict[str, np.ndarray]
    done: dict[str, np.ndarray]
    version: str
    path: Path
    run: Run | None = None
    census: ContextIndex | None = None
    code_digest: str = ""
    dirty: bool = False
    timings: dict[str, float] = field(default_factory=dict)
    extra: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    relation: dict[tuple[Any, ...], bool] = field(default_factory=dict)
    workers: int = 1
    export_digest: str = ""
    tagged: dict[str, dict[str, dict[str, np.ndarray]]] = field(default_factory=dict)
    facts: dict[str, dict[tuple[int, int], str | None]] = field(default_factory=dict)
    _index: dict[tuple[int, int], int] | None = None

    @property
    def n(self) -> int:
        return len(self.keys)

    def key_index(self) -> dict[tuple[int, int], int]:
        if self._index is None:
            self._index = {key: i for i, key in enumerate(self.keys)}
        return self._index

    @property
    def lo(self) -> np.ndarray:
        return np.fromiter((k[0] for k in self.keys), dtype=np.int64, count=self.n)

    @property
    def hi(self) -> np.ndarray:
        return np.fromiter((k[1] for k in self.keys), dtype=np.int64, count=self.n)

    def feats(self, i: int) -> Feats:
        return feats_of(self.V, self.P, i)

    def column(self, feature: str) -> tuple[np.ndarray, np.ndarray]:
        """A feature column and its presence: an engine feature, or an extra column an arm
        loaded (`load_extra`), such as a GPU job's per-pair image scores."""
        if feature in self.extra:
            return self.extra[feature]
        j = FIDX[feature]
        return self.V[:, j], self.P[:, j]

    def load_extra(self, path: str | Path) -> list[str]:
        """Per-pair columns from an `.npz` with `lo`, `hi` and one array per column (NaN =
        absent): the plug for any mechanic computed elsewhere (a GPU box, a notebook)."""
        data = np.load(path)
        index = {key: i for i, key in enumerate(self.keys)}
        rows = np.array([index.get((int(a), int(b)), -1) for a, b in zip(data["lo"], data["hi"])])
        hit = rows >= 0
        names = [name for name in data.files if name not in ("lo", "hi")]
        for name in names:
            values = np.zeros(self.n)
            present = np.zeros(self.n, dtype=bool)
            column = data[name].astype(np.float64)
            values[rows[hit]] = np.nan_to_num(column[hit])
            present[rows[hit]] = ~np.isnan(column[hit])
            self.extra[name] = (values, present)
        return names

    def settings_with(self, overrides: dict[str, Any] | None) -> Settings:
        """The artefact's settings row plus an arm's overrides (an unknown field raises)."""
        return dataclasses.replace(self.settings, **overrides) if overrides else self.settings

    def lazy(self, kind: str, idx: np.ndarray, overrides: dict[str, Any] | None = None
             ) -> dict[str, np.ndarray]:
        """The lazy signal columns of `kind` (gate / promote), filled for `idx`: the artefact's
        own under its settings row, or a sibling set computed by the SAME engine functions under
        the row plus `overrides`, kept in the overlay under the overrides' tag."""
        if not overrides:
            self.ensure(kind, idx)
            return {name: self.sig[name] for name in LAZY_SIGNALS[kind]}
        tag = json.dumps(overrides, sort_keys=True)
        slot = self.tagged.setdefault(tag, {"sig": {}, "done": {}})
        if kind not in slot["done"]:
            slot["done"][kind] = np.zeros(self.n, dtype=bool)
            for name in LAZY_SIGNALS[kind]:
                slot["sig"][name] = _objects([() if name == "gate" else ""] * self.n)
        self._fill(kind, idx, slot["sig"], slot["done"], self.settings_with(overrides))
        return {name: slot["sig"][name] for name in LAZY_SIGNALS[kind]}

    def ensure(self, kind: str, idx: np.ndarray) -> int:
        """Fill the lazy signal `kind` for the pairs in `idx` that lack it, with the engine
        function the artefact used (`evidence.LAZY_FN`); returns how many."""
        return self._fill(kind, idx, self.sig, self.done, self.settings)

    def _fill(self, kind: str, idx: np.ndarray, sig: dict[str, np.ndarray],
              done: dict[str, np.ndarray], settings: Settings) -> int:
        missing = idx[~done[kind][idx]]
        if len(missing) == 0:
            return 0
        order = [int(i) for i in missing]
        job = evidence.job(self.keys, _LazyFeats(self), self.fps, self.ds.listings, self.model,
                           settings)
        rows = evidence.pooled(job, evidence.LAZY_FN[kind], order,
                               self.workers if len(order) > 2000 else 1)
        for i, values in zip(order, rows):
            for name, value in zip(LAZY_SIGNALS[kind], values):
                sig[name][i] = value
        done[kind][missing] = True
        self.dirty = True
        return len(order)

    def save(self) -> None:
        """The overlay: lazy fills (per override tag), the relation memo and the challenger's
        fact memo (the artefact itself is never written)."""
        if not self.dirty:
            return
        names = [name for names in LAZY_SIGNALS.values() for name in names]
        payload = {"schema": OVERLAY_SCHEMA, "version": self.version,
                   "sig": {name: self.sig[name] for name in names}, "done": self.done,
                   "relation": self.relation, "tagged": self.tagged, "facts": self.facts}
        self.path.mkdir(parents=True, exist_ok=True)
        tmp = self.path / "overlay.pkl.tmp"
        with open(tmp, "wb") as handle:
            pickle.dump(payload, handle, protocol=5)
        tmp.replace(self.path / "overlay.pkl")
        self.dirty = False


class _LazyFeats:
    """`feats[i]` rebuilt from V/P on demand, so a fill never materialises every pair's dict."""

    def __init__(self, c: Cohort) -> None:
        self.c = c

    def __getitem__(self, i: int) -> Feats:
        return self.c.feats(i)


def from_evidence(name: str, spec: dict[str, Any], payload: dict[str, Any], ds: Dataset,
                  path: Path, workers: int = 1) -> Cohort:
    """The Cohort over one artefact (already read and checked)."""
    manifest = payload["manifest"]
    if list(manifest["feature_order"]) != list(FEATURE_ORDER):
        raise StaleEvidence(f"{name}: the artefact's feature order is not this checkout's")
    settings = Settings.from_dict(payload["settings"])
    model = LogisticModel.from_json(payload["model"])
    keys = [(int(lo), int(hi)) for lo, hi in payload["keys"]]
    n, width = len(keys), len(FEATURE_ORDER)
    V = np.frombuffer(payload["V"], dtype=np.float64).reshape(n, width).copy()
    P = np.frombuffer(payload["P"], dtype=np.uint8).reshape(n, width).astype(bool)
    raw = payload["signals"]
    sig: dict[str, np.ndarray] = {}
    for key, values in raw.items():
        if key == "score_ref":
            sig[key] = np.frombuffer(values, dtype=np.float64).copy()
        elif key == "nfam":
            sig[key] = np.array(values, dtype=np.int16)
        else:
            sig[key] = _objects(values)
    done = {kind: np.frombuffer(mask, dtype=np.uint8).astype(bool)
            for kind, mask in payload["done"].items()}
    decision = payload["decision"]
    run = Run(zone=np.array([ZONE_CODE[z] for z in decision["zone"]], dtype=np.int8),
              reason=_objects(decision["reason"]),
              score=np.frombuffer(decision["score"], dtype=np.float64).copy(),
              certificate=_objects(decision["certificate"]),
              stored=np.frombuffer(decision["stored"], dtype=np.uint8).astype(bool),
              dir=Path(spec["run"]))
    clock = time.perf_counter()
    fps = fingerprints(ds, settings)
    cohort = Cohort(name, spec, ds, settings, model, fps, keys,
                    [frozenset(p) for p in payload["probes"]], V, P, sig, done,
                    manifest["version"], path, run, payload.get("census"), manifest["code_digest"],
                    workers=workers, export_digest=manifest["export_digest"])
    cohort.timings["fingerprints_s"] = time.perf_counter() - clock
    return cohort


def check_seal(reg: dict[str, Any], spec: dict[str, Any]) -> None:
    """A sealed export (a preregistration's block) opens only when the registry names its
    freeze (`"freeze": "<the addendum>"` on the cohort); the registry's `seals` add files to
    the committed preregistrations."""
    evidence.check_seal(spec["export"], reg.get("seals") or (), freeze=bool(spec.get("freeze")))


def open_cohort(name: str, registry_path: str | Path | None = None, workers: int = 4,
                cache_root: str | Path | None = None) -> Cohort:
    """The cohort's artefact (written by `harness run --evidence`), checked against the seals,
    this checkout's engine code (on the manifest, before the pickle) and the registry's export,
    plus the lab's overlay if it has one."""
    reg = registry(registry_path)
    spec = reg["cohorts"][name]
    check_seal(reg, spec)
    root = Path(cache_root or reg["cache_root"])
    clock = time.perf_counter()
    manifest = evidence.read_manifest(spec["run"])
    code = evidence.code_digest()
    if manifest["code_digest"] != code:
        raise StaleEvidence(
            f"{name}: evidence at {spec['run']} was written by engine code {manifest['code_digest']}, "
            f"this checkout is {code}; re-run `harness run --evidence` on this code")
    export = evidence.file_digest(spec["export"], root / "digests.json")
    if export != manifest["export_digest"]:
        raise StaleEvidence(f"{name}: evidence was written off export {manifest['export_digest']}, "
                            f"the registry's export is {export}")
    payload = evidence.read(spec["run"])
    if payload["manifest"]["version"] != manifest["version"]:
        raise StaleEvidence(f"{name}: evidence.json and evidence.pkl disagree on the version")
    timings = {"evidence_s": time.perf_counter() - clock}
    clock = time.perf_counter()
    ds = load(spec["export"])
    timings["export_s"] = time.perf_counter() - clock
    cohort = from_evidence(name, spec, payload, ds, root / name / manifest["version"], workers)
    cohort.timings.update(timings)
    overlay = cohort.path / "overlay.pkl"
    if overlay.is_file():
        with open(overlay, "rb") as handle:
            saved = pickle.load(handle)
        if saved.get("schema") == OVERLAY_SCHEMA and saved.get("version") == cohort.version:
            cohort.sig.update(saved["sig"])
            cohort.done.update(saved["done"])
            cohort.relation = saved.get("relation", {})
            cohort.tagged = saved.get("tagged", {})
            cohort.facts = saved.get("facts", {})
    cohort.timings["load_s"] = sum(cohort.timings.values())
    return cohort
