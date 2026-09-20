"""Replay equivalence: the cohort fed through the incremental path, compared with the batch pass.

    python3 -m autodedup.replay --artifact cohort.jsonl.gz --settings autodedup/settings/w8.json \
        --model autodedup/models/w6_gold.json --out out/ [--shuffle-seed 7]

The claim this proves is narrow and mechanical: **at the same settings, the same model and the
same frozen calibration (E70), the incremental path's final state equals the cohort pass's.**
Pair decisions are pure functions of the pair under a frozen calibration, so any zone
difference is a retrieval or bookkeeping bug; clusters are recomputed per connected component
(E72), so any cluster difference is an ordering bug. Both are reported by cause rather than
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
    EVIDENCE_HOLD_REASON,
    Calibration,
    EvidenceHold,
    Limits,
    PassResult,
    WorkItem,
    run_pass_bounded,
)
from autodedup.incremental_scope import Scope, parse_scope
from autodedup.incremental_store import MemoryStore
from autodedup.blocking import generate_pairs
from autodedup.cluster import cluster_pairs
from autodedup.guards import UNIT_DESIGNATOR_VETO
from autodedup.model import LogisticModel
from autodedup.score_lane import storable
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


class WithheldFacts:
    """`DatasetFacts` that hands a listing's PHOTOGRAPHS over late (E92/E93).

    This is the shape production actually has and the plain replay cannot see: images land a
    minute after the advert, their pHash an hour later and their CLIP vector two to four hours
    after that, while the lane claims the listing within fifteen minutes. So the first claim
    sees a gallery with `phash`, `pop`, `clip` and `tags` all stripped — an honest gallery of
    unprocessed photographs, not an absent one — and `deliver` is the producers arriving."""

    def __init__(self, ds: Dataset) -> None:
        self.ds = ds
        self.reads = 0
        self.delivered: set[int] = set()

    def deliver_all(self) -> None:
        self.delivered = set(self.ds.listings)

    def facts(self, ids: Iterable[int]) -> dict[int, tuple[Listing, list[Image]]]:
        out: dict[int, tuple[Listing, list[Image]]] = {}
        for listing_id in ids:
            listing = self.ds.listings.get(listing_id)
            if listing is None:
                continue
            self.reads += 1
            images = self.ds.images(listing_id)
            if listing_id not in self.delivered:
                images = [replace(image, phash=None, pop=None, clip=None, tags=())
                          for image in images]
            out[listing_id] = (listing, images)
        return out


class RedecideWork:
    """The evidence sweep, as a schedule: the same ids handed back with `redecide` set.

    Production's sweep finds them by probing `public.images` against the counts on `rt_fp`;
    here the arrival of the photographs is known, so what is being replayed is what the sweep
    DOES with them — the pass re-writes the postings and re-scores the pairs even though, for
    a hold the horizon released rather than the evidence, no digest moved at all."""

    def __init__(self, order: Sequence[int]) -> None:
        self.order = list(order)
        self.cursor = 0

    def claim(self, limit: int) -> list[WorkItem]:
        batch = self.order[self.cursor:self.cursor + limit]
        return [WorkItem(listing_id, "evidence", None, position, redecide=True)
                for position, listing_id in enumerate(batch, start=self.cursor + 1)]

    def commit(self, done: Sequence[WorkItem]) -> dict[str, Any]:
        positions = [int(item.cursor) for item in done if item.cursor is not None]
        if positions:
            self.cursor = max(positions)
        return {"redecided": self.cursor, "total": len(self.order)}

    def exhausted(self) -> bool:
        return self.cursor >= len(self.order)


class ScheduleWork:
    """The WorkSource: an arrival schedule instead of the four watermark cursors.

    It obeys the same contract production's does — a claim advances nothing, a COMMIT advances
    over exactly the items it is handed — so a pass the pair budget refused (E75) re-claims the
    same arrivals here as it would there."""

    def __init__(self, order: Sequence[int]) -> None:
        self.order = list(order)
        self.cursor = 0

    def claim(self, limit: int) -> list[WorkItem]:
        batch = self.order[self.cursor:self.cursor + limit]
        return [WorkItem(listing_id, "new", None, position)
                for position, listing_id in enumerate(batch, start=self.cursor + 1)]

    def commit(self, done: Sequence[WorkItem]) -> dict[str, Any]:
        positions = [int(item.cursor) for item in done if item.cursor is not None]
        if positions:
            self.cursor = max(positions)
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
        before = work.cursor
        passes.append(run_pass_bounded(store, facts, work, settings, model, calibration,
                                       limits=limits, now=None))
        if work.cursor == before:
            # `run_pass_bounded` exhausted its retries without fitting the budget. Looping
            # again would spin, and pretending otherwise would hide it.
            raise SystemExit(
                f"pair budget refused {passes[-1].wanted_pairs} pairs at "
                f"max_pairs={limits.max_pairs}; raise it or lower --batch-size")
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
        "pair_budget_refusals": sum(1 for p in passes if p.attempts > 1),
        "pairs_deleted": sum(p.pairs_deleted for p in passes),
        "fact_reads": facts.reads,
        "components": sum(p.components for p in passes),
        "oversized_components": sum(len(p.oversized_components) for p in passes),
        "rail_reopened": sum(int(p.rail.get("reopened", 0)) for p in passes),
        "spent_usd": sum(p.spent_usd for p in passes),
    }
    return pairs, {key: list(members) for key, members in store.clusters.items()}, stats


# The withheld-evidence replay's clock. Nothing here is a wall clock: what matters is that
# the first decision is INSIDE the horizon (so the hold fires) and the last one is outside it
# (so the hold expires with the evidence still absent).
WITHHELD_T0: float = 1_780_000_000.0
WITHHELD_HORIZON_S: float = 48 * 3600.0


def withheld_state(
    ds: Dataset,
    settings: Settings,
    model: LogisticModel,
    calibration: Calibration,
    order: Sequence[int],
    batch_size: int,
    limits: Limits,
) -> tuple[dict[tuple[int, int], dict[str, Any]], dict[int, list[int]], dict[str, Any]]:
    """The incremental path run against production's actual evidence timeline (E92/E93).

    Three phases, and the claim is about the THIRD: every listing is first decided with its
    photographs unprocessed (and every photo-dependent merge therefore HELD in the band); the
    producers then land and the evidence sweep re-claims; and finally the horizon passes, so a
    hold that is still waiting on a gallery the cohort itself never hashed is released on what
    the lane has. The final state must be the batch engine's, exactly — the hold changes WHEN
    a decision is reached, never which one."""
    store = MemoryStore(now=WITHHELD_T0)
    facts = WithheldFacts(ds)
    hold = EvidenceHold(now=WITHHELD_T0, horizon_s=WITHHELD_HORIZON_S)
    passes: list[PassResult] = []
    clock = time.perf_counter()

    work = ScheduleWork(order)
    while not work.exhausted():
        before = work.cursor
        passes.append(run_pass_bounded(store, facts, work, settings, model, calibration,
                                       limits=limits, now=None, hold=hold))
        if work.cursor == before:
            raise SystemExit("pair budget refused the withheld-evidence replay")
    held_first = sum(p.held for p in passes)

    # The producers arrive. Production's sweep notices because the counts on `rt_fp` no longer
    # match a probe of `public.images`; here they are handed over directly.
    facts.deliver_all()
    sweep = RedecideWork(order)
    while not sweep.exhausted():
        before = sweep.cursor
        passes.append(run_pass_bounded(store, facts, sweep, settings, model, calibration,
                                       limits=limits, now=None, hold=hold))
        if sweep.cursor == before:
            raise SystemExit("pair budget refused the evidence sweep")

    # And the horizon passes, for the galleries the cohort itself never hashed.
    late = EvidenceHold(now=WITHHELD_T0 + WITHHELD_HORIZON_S + 3600.0,
                        horizon_s=WITHHELD_HORIZON_S)
    expiry = RedecideWork(order)
    while not expiry.exhausted():
        before = expiry.cursor
        passes.append(run_pass_bounded(store, facts, expiry, settings, model, calibration,
                                       limits=limits, now=None, hold=late))
        if expiry.cursor == before:
            raise SystemExit("pair budget refused the horizon sweep")

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
        "held_on_first_decision": held_first,
        "held_total": sum(p.held for p in passes),
        "released_total": sum(p.released for p in passes),
        "still_held": sum(1 for row in store.pairs.values()
                          if row.reason == EVIDENCE_HOLD_REASON),
        "pairs_scored": sum(p.pairs_scored for p in passes),
        "fact_reads": facts.reads,
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


def _stored(
    pairs: dict[tuple[int, int], dict[str, Any]], store_floor: float
) -> dict[tuple[int, int], dict[str, Any]]:
    """What the lane's store keeps of a decided set — the batch lane's own predicate."""
    return {key: row for key, row in pairs.items() if storable(row, store_floor)}


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autodedup.replay")
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--settings", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--out", default="out/")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--max-component", type=int, default=400)
    parser.add_argument("--max-pairs", type=int, default=Limits().max_pairs)
    parser.add_argument("--shuffle-seed", type=int, default=None)
    # E92/E93: re-run the same cohort with every listing's photographs WITHHELD at its first
    # claim and delivered afterwards. The final state must still be the batch engine's.
    parser.add_argument("--withhold-photos", action="store_true")
    # The real-time lane holds only `rt_scope` (E79), so the honest equivalence claim is over
    # the scope: the SAME listings on both sides, the batch pass included. Passing it here
    # restricts the dataset once, before either path sees it.
    parser.add_argument("--scope", default=None)
    ns = parser.parse_args(argv)

    settings = load_settings(ns.settings)
    model = load_model(ns.model)
    ds = load(ns.artifact)
    scope: Scope | None = parse_scope(ns.scope) if ns.scope else None
    if scope is not None and not scope.whole_corpus:
        kept = {i for i, listing in ds.listings.items() if scope.holds(listing)}
        if not kept:
            raise SystemExit(f"scope {scope.label()!r} holds none of this cohort")
        ds = Dataset(
            meta=ds.meta,
            listings={i: listing for i, listing in ds.listings.items() if i in kept},
            images_by_listing={i: images for i, images in ds.images_by_listing.items()
                               if i in kept},
            integrity=ds.integrity,
        )
    out_dir = Path(ns.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    fps = build_all(ds, settings)
    calibration = Calibration.build(fps, ds.listings, settings)

    reference, batch_clusters, timings = batch_state(ds, settings, model)
    # The SHIPPED budget, not an unreachable one: `max_pairs` is the only cap the cohort pass
    # has no equivalent of, so a replay that raised it out of reach would prove equivalence
    # for a configuration production never runs.
    limits = Limits(max_listings=ns.batch_size, max_pairs=ns.max_pairs,
                    max_component=ns.max_component)
    order = arrival_order(ds)
    pairs, clusters, stats = incremental_state(
        ds, settings, model, calibration, order, ns.batch_size, limits
    )

    report: dict[str, Any] = {
        "artifact": str(ns.artifact),
        "scope": scope.as_json() if scope is not None else None,
        "n_listings": len(ds.listings),
        "settings": settings.to_dict(),
        "model_version": model.version,
        "calibration_digest": calibration.digest(),
        "max_pairs": ns.max_pairs,
        "batch_size": ns.batch_size,
        "batch": {"pairs": len(reference), "clusters": len(batch_clusters), **timings},
        "incremental": stats,
        "pairs": compare_pairs(reference, pairs),
        # The STORED grain as well as the decided one: the SQL store keeps what
        # `score_lane.storable` keeps (merge, band, and the reject tail at or above
        # `store_floor`), so the retention rule is proved equivalent rather than assumed.
        "stored_pairs": compare_pairs(_stored(reference, settings.store_floor),
                                      _stored(pairs, settings.store_floor)),
        "clusters": compare_clusters(batch_clusters, clusters),
    }

    if ns.withhold_photos:
        w_pairs, w_clusters, w_stats = withheld_state(
            ds, settings, model, calibration, order, ns.batch_size, limits
        )
        report["withheld_photos"] = {
            "stats": w_stats,
            "pairs_vs_batch": compare_pairs(reference, w_pairs),
            "stored_pairs_vs_batch": compare_pairs(_stored(reference, settings.store_floor),
                                                   _stored(w_pairs, settings.store_floor)),
            "clusters_vs_batch": compare_clusters(batch_clusters, w_clusters),
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
