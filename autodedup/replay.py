"""Replay equivalence: the cohort fed through the incremental path, compared with the batch pass.

    python3 -m autodedup.replay --artifact cohort.jsonl.gz --settings autodedup/settings/w8.json \
        --model autodedup/models/w6_gold.json --out out/ [--shuffle-seed 7]

The claim this proves is narrow and mechanical: **at the same settings, the same model and the
same frozen calibration (E65), the incremental path's final state equals the cohort pass's.**
Pair decisions are pure functions of the pair under a frozen calibration, so any zone
difference is a retrieval or bookkeeping bug; clusters are recomputed per connected component
(E67), so any cluster difference is an ordering bug. Both are reported by cause rather than
counted, because a count cannot be debugged.

`--shuffle-seed` re-runs the same cohort with arrivals shuffled INSIDE each calendar day and
compares the two incremental runs with each other: order-insensitivity is a property of the
mechanism, and the only honest way to show it is to vary the order.
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from autodedup.dataset import Dataset, Image, Listing, load
from autodedup.decide import decide_pair
from autodedup.features import FeatureContext, pair_features
from autodedup.fingerprint import build_all
from autodedup.harness import load_model, load_settings
from autodedup.hazard_context import ContextIndex
from autodedup.incremental import (
    Calibration,
    Limits,
    PassResult,
    run_pass,
)
from autodedup.incremental_store import MemoryStore
from autodedup.blocking import generate_pairs
from autodedup.cluster import cluster_pairs
from autodedup.guards import UNIT_DESIGNATOR_VETO
from autodedup.model import LogisticModel
from autodedup.settings import Settings


class DatasetFacts:
    """The FactSource over an exported cohort: in production these two reads are
    `public.listings` (+ `listing_location`) and `public.images` (+ tags, CLIP)."""

    def __init__(self, ds: Dataset) -> None:
        self.ds = ds
        self.reads = 0

    def facts(self, ids: Iterable[int]) -> dict[int, tuple[Listing, list[Image]]]:
        out: dict[int, tuple[Listing, list[Image]]] = {}
        for listing_id in ids:
            listing = self.ds.listings.get(listing_id)
            if listing is None:
                continue
            self.reads += 1
            out[listing_id] = (listing, self.ds.images(listing_id))
        return out


class ScheduleWork:
    """The WorkSource: an arrival schedule instead of the three watermark cursors."""

    def __init__(self, order: Sequence[int]) -> None:
        self.order = list(order)
        self.cursor = 0

    def claim(self, limit: int) -> list[int]:
        batch = self.order[self.cursor:self.cursor + limit]
        self._claimed = len(batch)
        return list(batch)

    def commit(self) -> dict[str, Any]:
        self.cursor += getattr(self, "_claimed", 0)
        return {"arrivals_done": self.cursor, "arrivals_total": len(self.order)}

    def exhausted(self) -> bool:
        return self.cursor >= len(self.order)


def arrival_order(ds: Dataset, shuffle_seed: int | None = None) -> list[int]:
    """Listing ids in `first_seen_at` order; a seed shuffles WITHIN each calendar day.

    Ties break on the listing id, which is also the corpus's own arrival order, so the
    unshuffled schedule is the one production would see."""
    rows = [(listing.first_seen_at or "", listing.id) for listing in ds.listings.values()]
    rows.sort()
    if shuffle_seed is None:
        return [listing_id for _stamp, listing_id in rows]
    rng = random.Random(shuffle_seed)
    out: list[int] = []
    day = ""
    bucket: list[int] = []
    for stamp, listing_id in rows:
        if stamp[:10] != day:
            rng.shuffle(bucket)
            out.extend(bucket)
            bucket = []
            day = stamp[:10]
        bucket.append(listing_id)
    rng.shuffle(bucket)
    out.extend(bucket)
    return out


def batch_state(
    ds: Dataset, settings: Settings, model: LogisticModel
) -> tuple[dict[tuple[int, int], dict[str, Any]], dict[int, list[int]], dict[str, float]]:
    """The reference: the cohort pass's decisions and clusters, in this process."""
    timings: dict[str, float] = {}
    clock = time.perf_counter()
    fps = build_all(ds, settings)
    pairs, _stats = generate_pairs(fps, settings)
    ctx = FeatureContext.build(fps, settings, ds)
    ctx.index_attrs(fps, ds.listings)
    hazard = ContextIndex.build(ds.listings, ds.images_by_listing)
    decisions = []
    out: dict[tuple[int, int], dict[str, Any]] = {}
    vetoed: set[tuple[int, int]] = set()
    for (lo, hi) in sorted(pairs):
        fa, fb = fps[lo], fps[hi]
        la, lb = ds.listings[lo], ds.listings[hi]
        feats = pair_features(fa, fb, la, lb, ds.images(lo), ds.images(hi), ctx, settings)
        decision = decide_pair(fa, fb, la, lb, feats, pairs[(lo, hi)], model, settings, hazard)
        decisions.append(decision)
        if decision.veto == UNIT_DESIGNATOR_VETO:
            vetoed.add((lo, hi))
        out[(lo, hi)] = _pair_view(decision, sorted(pairs[(lo, hi)]))
    clustered = cluster_pairs(decisions, ds.listings, fps, settings, frozenset(vetoed))
    timings["batch_s"] = time.perf_counter() - clock
    return out, {key: list(members) for key, members in clustered.clusters.items()}, timings


def _pair_view(decision: Any, probes: Sequence[str]) -> dict[str, Any]:
    return {
        "zone": decision.zone,
        "score": round(float(decision.score), 9),
        "reason": decision.reason,
        "certificate": decision.certificate,
        "veto": decision.veto,
        "families": sorted(decision.families),
        "probes": list(probes),
    }


def incremental_state(
    ds: Dataset,
    settings: Settings,
    model: LogisticModel,
    calibration: Calibration,
    order: Sequence[int],
    batch_size: int,
    limits: Limits,
) -> tuple[dict[tuple[int, int], dict[str, Any]], dict[int, list[int]], dict[str, Any]]:
    store = MemoryStore()
    facts = DatasetFacts(ds)
    work = ScheduleWork(order)
    passes: list[PassResult] = []
    clock = time.perf_counter()
    while not work.exhausted():
        passes.append(run_pass(store, facts, work, settings, model, calibration,
                               limits=limits, now=None))
    elapsed = time.perf_counter() - clock
    pairs = {
        key: {
            "zone": row.zone,
            "score": round(float(row.score), 9),
            "reason": row.reason,
            "certificate": row.certificate,
            "veto": row.veto,
            "families": sorted(row.families),
            "probes": sorted(row.probes),
        }
        for key, row in store.pairs.items()
    }
    stats = {
        "passes": len(passes),
        "elapsed_s": round(elapsed, 3),
        "listings_per_s": round(len(order) / elapsed, 2) if elapsed else 0.0,
        "pairs_scored": sum(p.pairs_scored for p in passes),
        "pairs_deleted": sum(p.pairs_deleted for p in passes),
        "fact_reads": facts.reads,
        "components": sum(p.components for p in passes),
        "oversized_components": sum(len(p.oversized_components) for p in passes),
        "rail_reopened": sum(int(p.rail.get("reopened", 0)) for p in passes),
        "spent_usd": sum(p.spent_usd for p in passes),
    }
    return pairs, {key: list(members) for key, members in store.clusters.items()}, stats


def compare_pairs(
    left: dict[tuple[int, int], dict[str, Any]],
    right: dict[tuple[int, int], dict[str, Any]],
) -> dict[str, Any]:
    only_left = sorted(set(left) - set(right))
    only_right = sorted(set(right) - set(left))
    differing: list[dict[str, Any]] = []
    by_cause: dict[str, int] = {}
    for key in sorted(set(left) & set(right)):
        a, b = left[key], right[key]
        causes = sorted(field for field in a if a[field] != b[field])
        if not causes:
            continue
        by_cause["+".join(causes)] = by_cause.get("+".join(causes), 0) + 1
        if len(differing) < 25:
            differing.append({"pair": list(key), "batch": a, "incremental": b})
    return {
        "n_left": len(left),
        "n_right": len(right),
        "only_batch": len(only_left),
        "only_incremental": len(only_right),
        "only_batch_sample": [list(k) for k in only_left[:25]],
        "only_incremental_sample": [list(k) for k in only_right[:25]],
        "differing": sum(by_cause.values()),
        "differing_by_cause": dict(sorted(by_cause.items())),
        "differing_sample": differing,
        "identical": len(left) == len(right) and not only_left and not only_right
                     and not by_cause,
    }


def compare_clusters(left: dict[int, list[int]], right: dict[int, list[int]]) -> dict[str, Any]:
    left_sets = {tuple(sorted(members)) for members in left.values()}
    right_sets = {tuple(sorted(members)) for members in right.values()}
    only_left = sorted(left_sets - right_sets)
    only_right = sorted(right_sets - left_sets)
    return {
        "n_batch": len(left),
        "n_incremental": len(right),
        "keys_identical": {int(k): v for k, v in sorted(left.items())}
                          == {int(k): v for k, v in sorted(right.items())},
        "member_sets_identical": not only_left and not only_right,
        "only_batch": len(only_left),
        "only_incremental": len(only_right),
        "only_batch_sample": [list(s) for s in only_left[:15]],
        "only_incremental_sample": [list(s) for s in only_right[:15]],
    }


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autodedup.replay")
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--settings", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--out", default="out/")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--max-component", type=int, default=400)
    parser.add_argument("--shuffle-seed", type=int, default=None)
    ns = parser.parse_args(argv)

    settings = load_settings(ns.settings)
    model = load_model(ns.model)
    ds = load(ns.artifact)
    out_dir = Path(ns.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    fps = build_all(ds, settings)
    calibration = Calibration.build(fps, ds.listings, settings)

    reference, batch_clusters, timings = batch_state(ds, settings, model)
    limits = Limits(max_listings=ns.batch_size, max_pairs=10 ** 9,
                    max_component=ns.max_component)
    order = arrival_order(ds)
    pairs, clusters, stats = incremental_state(
        ds, settings, model, calibration, order, ns.batch_size, limits
    )

    report: dict[str, Any] = {
        "artifact": str(ns.artifact),
        "n_listings": len(ds.listings),
        "settings": settings.to_dict(),
        "model_version": model.version,
        "calibration_digest": calibration.digest(),
        "batch": {"pairs": len(reference), "clusters": len(batch_clusters), **timings},
        "incremental": stats,
        "pairs": compare_pairs(reference, pairs),
        "clusters": compare_clusters(batch_clusters, clusters),
    }

    if ns.shuffle_seed is not None:
        shuffled = arrival_order(ds, ns.shuffle_seed)
        s_pairs, s_clusters, s_stats = incremental_state(
            ds, settings, model, calibration, shuffled, ns.batch_size, limits
        )
        report["shuffled"] = {
            "seed": ns.shuffle_seed,
            "stats": s_stats,
            "pairs_vs_ordered": compare_pairs(pairs, s_pairs),
            "clusters_vs_ordered": compare_clusters(clusters, s_clusters),
        }

    (out_dir / "replay.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in report.items() if k != "settings"}, indent=2,
                     sort_keys=True)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(run())
