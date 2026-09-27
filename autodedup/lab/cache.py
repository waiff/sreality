"""One cohort's evidence cache: pairs, the 60 features and every rung's per-pair signal, computed
ONCE with the engine's own functions and stamped with a version, so an experiment only combines.

A signal is what one engine function says about one pair under the cache's settings row: the
wall or E61 veto, the auto-reject, the certificate, the E11 family count, the E46/E47 block, the
E63 warrant and its fungible refusal, the D43 gate's facts, the promotion warrant, the D50
refusal and the (B) corroboration. The two expensive ones (gate, promotion) are computed for the
pairs a ladder can reach at the cache's store floor and filled lazily for any other pair an arm
reaches; a fill is saved back, so the cache only grows. The version hashes the export, the
settings row, the feature file and every engine module: any change is a new cache, never a
stale one."""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import time
from dataclasses import dataclass, field
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from autodedup.blocking import generate_pairs
from autodedup.dataset import Dataset, Listing, load
from autodedup.decide import (
    auto_reject_reason,
    certificate_of,
    context_rule_warrant,
    demonstration_refusal,
    merge_zone_block,
    _census_limbs_configured,
)
from autodedup.demonstrate import corroboration_warrant
from autodedup.features import ABSENT, FEATURE_ORDER, FeatureContext, Feats, evidence_families, pair_features
from autodedup.fingerprint import Fingerprint, build_all
from autodedup.guards import UNIT_DESIGNATOR_VETO, pair_veto, unit_designator_conflict
from autodedup.hazard_context import ContextIndex, fungible_catalogue
from autodedup.indistinguishable import GATE, distinguishing_facts, promotion_warrant
from autodedup.model import LogisticModel
from autodedup.settings import Settings

SCHEMA: int = 2
ENGINE_DIR: Path = Path(__file__).resolve().parents[1]
REPO: Path = ENGINE_DIR.parent
FIDX: dict[str, int] = {name: i for i, name in enumerate(FEATURE_ORDER)}
BASE_SIGNALS: tuple[str, ...] = ("veto", "auto", "cert", "nfam", "block", "ctx_arm", "ctx_refused",
                                 "score_ref")
LAZY_SIGNALS: dict[str, tuple[str, ...]] = {"gate": ("gate",),
                                            "promote": ("warrant", "refusal", "corro")}


def registry(path: str | Path | None = None) -> dict[str, Any]:
    """The cohort registry: export, feature file, reference run and the label files, by name."""
    raw = path or os.environ.get("AUTODEDUP_LAB_COHORTS")
    if not raw:
        raise SystemExit("no cohort registry: pass --cohorts or set AUTODEDUP_LAB_COHORTS")
    return json.loads(Path(raw).read_text(encoding="utf-8"))


def _sha(parts: Sequence[bytes]) -> str:
    digest = hashlib.sha1()
    for part in parts:
        digest.update(part)
        digest.update(b"\0")
    return digest.hexdigest()


def code_digest() -> str:
    """Every engine module the signals read (the lab itself excluded)."""
    return _sha([p.name.encode() + p.read_bytes() for p in sorted(ENGINE_DIR.glob("*.py"))])[:12]


def file_digest(path: Path, memo: Path | None = None) -> str:
    """The file's CONTENT (sha1), so a cache built on one machine keeps its version on another.
    Hashing a 1-2 GB export takes seconds, so the digest is memoised in `memo` (one JSON under
    the cache root, never beside another job's files) against the file's size and mtime."""
    stat = path.stat()
    key, stamp = str(path.resolve()), f"{stat.st_size}:{stat.st_mtime_ns}"
    known: dict[str, list[str]] = {}
    if memo is not None and memo.is_file():
        try:
            known = json.loads(memo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            known = {}
        if known.get(key, [None])[0] == stamp:
            return known[key][1]
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    out = digest.hexdigest()[:12]
    if memo is not None:
        known[key] = [stamp, out]
        memo.parent.mkdir(parents=True, exist_ok=True)
        tmp = memo.with_suffix(".tmp")
        tmp.write_text(json.dumps(known, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, memo)
    return out


def feats_of(V: np.ndarray, P: np.ndarray, i: int) -> Feats:
    return {name: ((float(V[i, j]), True) if P[i, j] else ABSENT)
            for j, name in enumerate(FEATURE_ORDER)}


@dataclass
class Cohort:
    """A cohort as the lab holds it: the engine's own objects plus the cached signal columns."""

    name: str
    spec: dict[str, Any]
    ds: Dataset
    settings: Settings
    model: LogisticModel
    fps: dict[int, Fingerprint]
    hazard: ContextIndex
    keys: list[tuple[int, int]]
    probes: list[frozenset]
    V: np.ndarray
    P: np.ndarray
    sig: dict[str, np.ndarray]
    done: dict[str, np.ndarray]
    version: str
    path: Path
    dirty: bool = False
    timings: dict[str, float] = field(default_factory=dict)
    extra: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    relation: dict[tuple[tuple[int, int], bool, bool], bool] = field(default_factory=dict)
    workers: int = 1

    @property
    def n(self) -> int:
        return len(self.keys)

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

    def ensure(self, kind: str, idx: np.ndarray) -> int:
        """Fill the lazy signal `kind` for the pairs in `idx` that lack it; returns how many."""
        missing = idx[~self.done[kind][idx]]
        if len(missing) == 0:
            return 0
        fn = _gate_one if kind == "gate" else _promote_one
        global _COHORT
        _COHORT = self
        rows = _pooled(fn, [int(i) for i in missing], self.workers if len(missing) > 2000 else 1)
        for i, values in zip(missing, rows):
            for name, value in zip(LAZY_SIGNALS[kind], values):
                self.sig[name][i] = value
        self.done[kind][missing] = True
        self.dirty = True
        return len(missing)

    def save(self) -> None:
        if not self.dirty:
            return
        payload = {"sig": self.sig, "done": self.done, "relation": self.relation}
        tmp = self.path / "signals.pkl.tmp"
        with open(tmp, "wb") as handle:
            pickle.dump(payload, handle, protocol=5)
        tmp.replace(self.path / "signals.pkl")
        self.dirty = False


# --- the per-pair signal functions (module level so a forked pool can run them) ----------------

_COHORT: Cohort | None = None


def _pair(i: int) -> tuple[Fingerprint, Fingerprint, Listing, Listing, Feats]:
    c = _COHORT
    assert c is not None
    lo, hi = c.keys[i]
    return c.fps[lo], c.fps[hi], c.ds.listings[lo], c.ds.listings[hi], c.feats(i)


def _base_one(i: int) -> tuple[Any, ...]:
    c = _COHORT
    assert c is not None
    fa, fb, la, lb, feats = _pair(i)
    s = c.settings
    veto = pair_veto(fa, fb, s)
    if veto is None and unit_designator_conflict(la, lb, s) is not None:
        veto = UNIT_DESIGNATOR_VETO
    arm = context_rule_warrant(feats, 1.0, s) or ""
    refused = ""
    if arm:
        context = c.hazard.pair_context(la, lb)
        if context is None:
            refused = "unanswered" if _census_limbs_configured(s) else ""
        else:
            refused = fungible_catalogue(
                context.stamp, context.from_price, block_min=s.context_rule_block_min,
                image_population_min=s.context_rule_image_population_min,
                from_price_veto=s.context_rule_from_price_veto) or ""
    return (veto or "", auto_reject_reason(feats, s) or "",
            certificate_of(feats, la, lb, s, False) or "", len(evidence_families(feats)),
            merge_zone_block(feats, s) or "", arm, refused, c.model.predict_proba(feats))


def _gate_one(i: int) -> tuple[Any, ...]:
    c = _COHORT
    assert c is not None
    _, _, la, lb, feats = _pair(i)
    return (tuple(f.name for f in distinguishing_facts(la, lb, feats, c.settings, GATE)),)


def _promote_one(i: int) -> tuple[Any, ...]:
    c = _COHORT
    assert c is not None
    _, _, la, lb, feats = _pair(i)
    warrant = promotion_warrant(la, lb, feats, c.settings)
    if warrant is None:
        return ("", "", "")
    refusal = demonstration_refusal(la, lb, feats, c.settings) or ""
    corro = corroboration_warrant(la, lb, feats, c.settings) or ""
    return (warrant, refusal, corro)


def _chunk(args: tuple[Callable[[int], tuple[Any, ...]], list[int]]) -> list[tuple[Any, ...]]:
    fn, idx = args
    return [fn(i) for i in idx]


def _pooled(fn: Callable[[int], tuple[Any, ...]], idx: Sequence[int], workers: int
            ) -> list[tuple[Any, ...]]:
    chunks = [list(idx[k:k + 400]) for k in range(0, len(idx), 400)]
    if workers <= 1 or len(chunks) <= 1:
        return [row for chunk in chunks for row in _chunk((fn, chunk))]
    with get_context("fork").Pool(workers) as pool:
        parts = pool.map(_chunk, [(fn, chunk) for chunk in chunks], chunksize=1)
    return [row for part in parts for row in part]


# --- opening and building ------------------------------------------------------------------------

def _engine_objects(spec: dict[str, Any]) -> tuple[Dataset, Settings, LogisticModel,
                                                     dict[int, Fingerprint], ContextIndex]:
    ds = load(spec["export"])
    settings = Settings.from_json(REPO / spec["settings"])
    model = LogisticModel.from_json(json.loads((REPO / spec["model"]).read_text(encoding="utf-8")))
    fps = build_all(ds, settings)
    return ds, settings, model, fps, ContextIndex.build(ds.listings, ds.images_by_listing)


def _features(spec: dict[str, Any], ds: Dataset, settings: Settings,
              fps: dict[int, Fingerprint]) -> tuple[list, list, np.ndarray, np.ndarray]:
    path = Path(spec["features"]) if spec.get("features") else None
    if path is not None and path.is_file():
        with open(path, "rb") as handle:
            keys, probes, V, P = pickle.load(handle)
        return list(keys), list(probes), V, P
    pairs, _ = generate_pairs(fps, settings)
    ctx = FeatureContext.build(fps, settings, ds)
    ctx.index_attrs(fps, ds.listings)
    keys = sorted(pairs)
    V = np.zeros((len(keys), len(FEATURE_ORDER)), dtype=np.float64)
    P = np.zeros((len(keys), len(FEATURE_ORDER)), dtype=bool)
    for i, (lo, hi) in enumerate(keys):
        feats = pair_features(fps[lo], fps[hi], ds.listings[lo], ds.listings[hi],
                              ds.images(lo), ds.images(hi), ctx, settings)
        for j, name in enumerate(FEATURE_ORDER):
            value, present = feats.get(name, ABSENT)
            V[i, j], P[i, j] = float(value), bool(present)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            pickle.dump((keys, [frozenset(pairs[k]) for k in keys], V, P), handle, protocol=5)
    return keys, [frozenset(pairs[k]) for k in keys], V, P


def version_of(name: str, spec: dict[str, Any], memo: Path | None = None) -> str:
    parts = [str(SCHEMA).encode(), name.encode(),
             file_digest(Path(spec["export"]), memo).encode(),
             (REPO / spec["settings"]).read_bytes(), (REPO / spec["model"]).read_bytes(),
             code_digest().encode()]
    if spec.get("features"):
        parts.append(file_digest(Path(spec["features"]), memo).encode())
    return _sha(parts)[:12]


def open_cohort(name: str, registry_path: str | Path | None = None, workers: int = 4,
                cache_root: str | Path | None = None) -> Cohort:
    """The cohort's cache, built on first use (minutes), loaded thereafter (seconds)."""
    reg = registry(registry_path)
    spec = reg["cohorts"][name]
    root = Path(cache_root or reg["cache_root"])
    version = version_of(name, spec, root / "digests.json")
    path = root / name / version
    clock = time.perf_counter()
    ds, settings, model, fps, hazard = _engine_objects(spec)
    keys, probes, V, P = _features(spec, ds, settings, fps)
    timings = {"load_s": time.perf_counter() - clock}
    cohort = Cohort(name, spec, ds, settings, model, fps, hazard, keys, probes, V, P, {}, {},
                    version, path, timings=timings, workers=workers)
    signals = path / "signals.pkl"
    if signals.is_file():
        with open(signals, "rb") as handle:
            payload = pickle.load(handle)
        cohort.sig, cohort.done = payload["sig"], payload["done"]
        cohort.relation = payload.get("relation", {})
        cohort.timings["signals_s"] = time.perf_counter() - clock - timings["load_s"]
        return cohort
    _build_signals(cohort, workers)
    path.mkdir(parents=True, exist_ok=True)
    (path / "manifest.json").write_text(json.dumps({
        "schema": SCHEMA, "cohort": name, "version": version, "spec": spec,
        "code_digest": code_digest(), "pairs": cohort.n, "listings": len(ds.listings),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "timings": cohort.timings,
        "reachable": int(cohort.done["gate"].sum()),
    }, indent=1, sort_keys=True), encoding="utf-8")
    cohort.dirty = True
    cohort.save()
    return cohort


def _build_signals(cohort: Cohort, workers: int) -> None:
    global _COHORT
    _COHORT = cohort
    n = cohort.n
    clock = time.perf_counter()
    rows = _pooled(_base_one, list(range(n)), workers)
    for k, name in enumerate(BASE_SIGNALS):
        dtype = np.float64 if name == "score_ref" else (np.int16 if name == "nfam" else object)
        cohort.sig[name] = np.array([row[k] for row in rows], dtype=dtype)
    cohort.timings["base_signals_s"] = time.perf_counter() - clock
    print(f"[{cohort.name}] base signals {n} pairs {cohort.timings['base_signals_s']:.0f}s",
          flush=True)
    reach = np.flatnonzero((cohort.sig["veto"] == "") & (cohort.sig["auto"] == "")
                           & ((cohort.sig["cert"] != "")
                              | (cohort.sig["score_ref"] >= cohort.settings.store_floor)))
    for kind, names in LAZY_SIGNALS.items():
        clock = time.perf_counter()
        for name in names:
            cohort.sig[name] = np.empty(n, dtype=object)
            cohort.sig[name].fill(() if name == "gate" else "")
        cohort.done[kind] = np.zeros(n, dtype=bool)
        values = _pooled(_gate_one if kind == "gate" else _promote_one, list(reach), workers)
        for i, row in zip(reach, values):
            for name, value in zip(names, row):
                cohort.sig[name][i] = value
        cohort.done[kind][reach] = True
        cohort.timings[f"{kind}_signals_s"] = time.perf_counter() - clock
        print(f"[{cohort.name}] {kind} signals {len(reach)} pairs "
              f"{cohort.timings[kind + '_signals_s']:.0f}s", flush=True)
