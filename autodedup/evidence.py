"""The per-pair evidence `harness run --evidence` leaves beside its stored rows (B-a).

For EVERY pair the run decided (stored or not): its probes, its feature vector (V values, P
presence, FEATURE_ORDER), the decision the run took, and what each rung's engine function says
about it under the run's settings row. The lab reads this artefact and nothing else, so its pairs
are the one path's retrieval and its signals are the engine's own values: there is no second
retrieval path for the lab to drift on.

A signal is one engine function's answer for one pair: the wall or E61 veto, the auto-reject, the
certificate, the E11 family count, the E46/E47 block, the E63 warrant and its fungible refusal (off
the lane's census), the model score, the D43 gate's facts, the promotion warrant, the D50 refusal
and the (B) corroboration. The two expensive families (gate, promote) are computed for the pairs a
ladder can reach at the run's store floor; the lab fills any other pair it reaches with these same
functions. The version hashes the export, the settings row, the model and every module the run
imports (`code_digest`): an engine change is a new version, never a stale read."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import pickle
import time
from array import array
from dataclasses import dataclass
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from autodedup.dataset import Dataset, Listing
from autodedup.decide import (
    _census_limbs_configured,
    auto_reject_reason,
    certificate_of,
    context_rule_warrant,
    demonstration_refusal,
    merge_zone_block,
)
from autodedup.demonstrate import corroboration_warrant
from autodedup.features import FEATURE_ORDER, Feats, evidence_families
from autodedup.fingerprint import Fingerprint
from autodedup.guards import UNIT_DESIGNATOR_VETO, pair_veto, unit_designator_conflict
from autodedup.hazard_context import ContextIndex, fungible_catalogue
from autodedup.indistinguishable import GATE, distinguishing_facts, promotion_warrant
from autodedup.model import LogisticModel
from autodedup.settings import Settings
from autodedup.store_score import storable

SCHEMA: int = 1
FILE: str = "evidence.pkl"
MANIFEST: str = "evidence.json"
REPO: Path = Path(__file__).resolve().parents[1]
DIGEST_ROOTS: tuple[str, ...] = ("autodedup/harness.py", "autodedup/evidence.py")
BASE_SIGNALS: tuple[str, ...] = ("veto", "auto", "cert", "nfam", "block", "ctx_arm", "ctx_refused",
                                 "score_ref")
LAZY_SIGNALS: dict[str, tuple[str, ...]] = {"gate": ("gate",),
                                            "promote": ("warrant", "refusal", "corro")}


def _module_file(name: str) -> Path | None:
    parts = name.split(".")
    for candidate in (REPO.joinpath(*parts).with_suffix(".py"),
                      REPO.joinpath(*parts, "__init__.py")):
        if candidate.is_file():
            return candidate
    return None


def engine_modules(roots: Sequence[str] = DIGEST_ROOTS) -> list[str]:
    """The repo modules a run imports, transitively (static, so it cannot depend on what else a
    process happened to import): `toolkit/room_taxonomy.py` and every module `decide_pair` reads.
    The lab is excluded (editing a rung never re-versions evidence); package `__init__` files are
    not followed (autodedup's is empty, toolkit's is a lazy registry)."""
    seen: set[Path] = set()
    todo = [REPO / root for root in roots]
    while todo:
        path = todo.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
            for name in names:
                if name == "autodedup.lab" or name.startswith("autodedup.lab."):
                    continue
                found = _module_file(name)
                if found is not None and found.name != "__init__.py":
                    todo.append(found)
    return sorted(str(p.relative_to(REPO)) for p in seen)


def _sha(parts: Sequence[bytes]) -> str:
    digest = hashlib.sha1()
    for part in parts:
        digest.update(part)
        digest.update(b"\0")
    return digest.hexdigest()


def code_digest(roots: Sequence[str] = DIGEST_ROOTS) -> str:
    return _sha([rel.encode() + (REPO / rel).read_bytes()
                 for rel in engine_modules(roots)])[:12]


def file_digest(path: str | Path, memo: Path | None = None) -> str:
    """The file's CONTENT (sha1), memoised in `memo` against size and mtime, so a 1-2 GB export
    is hashed once per machine and a version built on one machine holds on another."""
    path = Path(path)
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


def settings_bytes(settings: Settings) -> bytes:
    return json.dumps(settings.to_dict(), sort_keys=True, default=list).encode()


def version_of(export_digest: str, settings: Settings, model: LogisticModel, code: str) -> str:
    return _sha([str(SCHEMA).encode(), export_digest.encode(), settings_bytes(settings),
                 json.dumps(model.to_json(), sort_keys=True).encode(), code.encode()])[:12]


# --- the per-pair signals: one engine function each ------------------------------------------

def base_signals(fa: Fingerprint, fb: Fingerprint, la: Listing, lb: Listing, feats: Feats,
                 model: LogisticModel, settings: Settings, census: ContextIndex
                 ) -> tuple[Any, ...]:
    """BASE_SIGNALS for one pair. The E63 warrant is read at score 1.0: its score bar is the
    rung's to apply, because an arm may score the pair differently."""
    veto = pair_veto(fa, fb, settings)
    if veto is None and unit_designator_conflict(la, lb, settings) is not None:
        veto = UNIT_DESIGNATOR_VETO
    arm = context_rule_warrant(feats, 1.0, settings) or ""
    refused = ""
    if arm:
        context = census.pair_context(la, lb)
        if context is None:
            refused = "unanswered" if _census_limbs_configured(settings) else ""
        else:
            refused = fungible_catalogue(
                context.stamp, context.from_price, block_min=settings.context_rule_block_min,
                image_population_min=settings.context_rule_image_population_min,
                from_price_veto=settings.context_rule_from_price_veto) or ""
    return (veto or "", auto_reject_reason(feats, settings) or "",
            certificate_of(feats, la, lb, settings, False) or "", len(evidence_families(feats)),
            merge_zone_block(feats, settings) or "", arm, refused, model.predict_proba(feats))


def gate_signals(la: Listing, lb: Listing, feats: Feats, settings: Settings) -> tuple[Any, ...]:
    return (tuple(f.name for f in distinguishing_facts(la, lb, feats, settings, GATE)),)


def promote_signals(la: Listing, lb: Listing, feats: Feats, settings: Settings
                    ) -> tuple[Any, ...]:
    warrant = promotion_warrant(la, lb, feats, settings)
    if warrant is None:
        return ("", "", "")
    refusal = demonstration_refusal(la, lb, feats, settings) or ""
    corro = corroboration_warrant(la, lb, feats, settings) or ""
    return (warrant, refusal, corro)


def reachable(sig: Mapping[str, Sequence[Any]], settings: Settings) -> list[int]:
    """The pairs the reference ladder can carry past the rule floor: not vetoed, not
    auto-rejected, and certified or at or above the store floor."""
    return [i for i, (veto, auto, cert, score) in enumerate(
        zip(sig["veto"], sig["auto"], sig["cert"], sig["score_ref"]))
        if veto == "" and auto == "" and (cert != "" or score >= settings.store_floor)]


# --- the pool (fork: the workers read the parent's objects, never pickled) ---------------------

@dataclass
class _Job:
    keys: list[tuple[int, int]]
    feats: list[Feats]
    fps: Mapping[int, Fingerprint]
    listings: Mapping[int, Listing]
    model: LogisticModel
    settings: Settings
    census: ContextIndex | None


_JOB: _Job | None = None


def _base(i: int) -> tuple[Any, ...]:
    job = _JOB
    assert job is not None and job.census is not None
    lo, hi = job.keys[i]
    return base_signals(job.fps[lo], job.fps[hi], job.listings[lo], job.listings[hi],
                        job.feats[i], job.model, job.settings, job.census)


def _gate(i: int) -> tuple[Any, ...]:
    job = _JOB
    assert job is not None
    lo, hi = job.keys[i]
    return gate_signals(job.listings[lo], job.listings[hi], job.feats[i], job.settings)


def _promote(i: int) -> tuple[Any, ...]:
    job = _JOB
    assert job is not None
    lo, hi = job.keys[i]
    return promote_signals(job.listings[lo], job.listings[hi], job.feats[i], job.settings)


LAZY_FN: dict[str, Callable[[int], tuple[Any, ...]]] = {"gate": _gate, "promote": _promote}


def _chunk(args: tuple[Callable[[int], tuple[Any, ...]], list[int]]) -> list[tuple[Any, ...]]:
    fn, idx = args
    return [fn(i) for i in idx]


def pooled(job: _Job, fn: Callable[[int], tuple[Any, ...]], idx: Sequence[int], workers: int
           ) -> list[tuple[Any, ...]]:
    """`fn` over `idx` in order, forked across `workers` when there is enough to share."""
    global _JOB
    _JOB = job
    chunks = [list(idx[k:k + 400]) for k in range(0, len(idx), 400)]
    if workers <= 1 or len(chunks) <= 1:
        return [row for chunk in chunks for row in _chunk((fn, chunk))]
    with get_context("fork").Pool(workers) as pool:
        parts = pool.map(_chunk, [(fn, chunk) for chunk in chunks], chunksize=1)
    return [row for part in parts for row in part]


def job(keys: list[tuple[int, int]], feats: list[Feats], fps: Mapping[int, Fingerprint],
        listings: Mapping[int, Listing], model: LogisticModel, settings: Settings,
        census: ContextIndex | None = None) -> _Job:
    return _Job(keys, feats, fps, listings, model, settings, census)


# --- the artefact -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Request:
    """What `harness run --evidence` was asked for: the export's content digest (part of the
    version), the fork width for the signal computation, and the engine code digest taken when
    the run STARTED (a file edited while a run is in flight must not re-label its output)."""

    export_digest: str
    workers: int = 1
    code_digest: str = ""


def write(out_dir: Path, rows: Mapping[tuple[int, int], Any], dataset: Dataset,
          fps: Mapping[int, Fingerprint], census: ContextIndex, settings: Settings,
          model: LogisticModel, request: Request) -> dict[str, Any]:
    """Every decided pair of the run's final store (`rows`: key -> PairRow) with its V, P, probes,
    decision and signals, plus the census the E63 refusals were read off, as `evidence.pkl`, and
    its manifest as `evidence.json`."""
    clock = time.perf_counter()
    keys = sorted(rows)
    feats: list[Feats] = []
    width = len(FEATURE_ORDER)
    V = array("d", bytes(8 * width * len(keys)))
    P = bytearray(width * len(keys))
    stray: set[str] = set()
    for i, key in enumerate(keys):
        row = rows[key]
        if row.feats is None:
            raise ValueError(f"pair {key} carries no feature vector: evidence needs the twin's rows")
        stray |= set(row.feats) - set(FEATURE_ORDER)
        feats.append(row.feats)
        for j, name in enumerate(FEATURE_ORDER):
            value, present = row.feats.get(name, (0.0, False))
            V[i * width + j] = float(value)
            P[i * width + j] = 1 if present else 0
    if stray:
        raise ValueError(f"features outside FEATURE_ORDER would not reach the lab: {sorted(stray)}")
    decision = {
        "zone": [rows[k].zone for k in keys], "reason": [rows[k].reason for k in keys],
        "score": array("d", (float(rows[k].score) for k in keys)),
        "certificate": [rows[k].certificate or "" for k in keys],
        "veto": [rows[k].veto or "" for k in keys],
        "stored": bytes(1 if storable({"zone": rows[k].zone, "score": rows[k].score,
                                       "evidence": rows[k].evidence}, settings.store_floor)
                        else 0 for k in keys)}
    work = job(keys, feats, fps, dataset.listings, model, settings, census)
    timings: dict[str, float] = {"vectors_s": time.perf_counter() - clock}
    clock = time.perf_counter()
    base = pooled(work, _base, range(len(keys)), request.workers)
    signals: dict[str, Any] = {name: [row[k] for row in base]
                               for k, name in enumerate(BASE_SIGNALS)}
    signals["score_ref"] = array("d", signals["score_ref"])
    signals["nfam"] = array("h", signals["nfam"])
    timings["base_s"] = time.perf_counter() - clock
    reach = reachable(signals, settings)
    done: dict[str, bytes] = {}
    for kind, names in LAZY_SIGNALS.items():
        clock = time.perf_counter()
        values = pooled(work, LAZY_FN[kind], reach, request.workers)
        for name in names:
            signals[name] = [() if name == "gate" else ""] * len(keys)
        for i, row in zip(reach, values):
            for name, value in zip(names, row):
                signals[name][i] = value
        mask = bytearray(len(keys))
        for i in reach:
            mask[i] = 1
        done[kind] = bytes(mask)
        timings[f"{kind}_s"] = time.perf_counter() - clock
    code = request.code_digest or code_digest()
    manifest = {
        "schema": SCHEMA, "version": version_of(request.export_digest, settings, model, code),
        "code_digest": code, "engine_modules": engine_modules(),
        "export_digest": request.export_digest, "model_version": model.version,
        "pairs": len(keys), "stored": sum(decision["stored"]), "reachable": len(reach),
        "listings": len(dataset.listings), "feature_order": list(FEATURE_ORDER),
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "timings": timings,
    }
    payload = {"manifest": manifest, "settings": settings.to_dict(), "model": model.to_json(),
               "census": census, "keys": keys, "probes": [list(rows[k].probes) for k in keys],
               "V": V, "P": bytes(P), "decision": decision, "signals": signals, "done": done}
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / (FILE + ".tmp")
    with open(tmp, "wb") as handle:
        pickle.dump(payload, handle, protocol=5)
    tmp.replace(out_dir / FILE)
    manifest["timings"]["total_s"] = sum(timings.values())
    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True),
                                    encoding="utf-8")
    return manifest


def read(run_dir: str | Path) -> dict[str, Any]:
    path = Path(run_dir) / FILE
    if not path.is_file():
        raise FileNotFoundError(f"{path}: run `harness run ... --evidence` to write it")
    with open(path, "rb") as handle:
        payload = pickle.load(handle)
    if payload["manifest"]["schema"] != SCHEMA:
        raise ValueError(f"{path}: evidence schema {payload['manifest']['schema']} != {SCHEMA}")
    return payload
