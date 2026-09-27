"""The tests' oracle for the incremental pass: every generated pair decided over the whole
cohort at once, then clustered once — what `run_pass` must reach whatever the arrival order.

Test-only. The engine has one path (`harness run` is `run_pass`, SW1); this is the independent
reference the E70/E71/E72 invariants are asserted against, and nothing ships it."""

from __future__ import annotations

import random
from typing import Any, Mapping

from autodedup.blocking import generate_pairs
from autodedup.cluster import cluster_pairs
from autodedup.d43 import relation_for
from autodedup.dataset import Dataset
from autodedup.decide import decide_pair
from autodedup.features import FeatureContext, pair_features
from autodedup.fingerprint import build_all
from autodedup.guards import UNIT_DESIGNATOR_VETO
from autodedup.hazard_context import ContextIndex, ContextStamp
from autodedup.indistinguishable import FEATURE_SLOTS
from autodedup.model import LogisticModel
from autodedup.settings import Settings
from autodedup.store_score import storable

# What the lane adds to a row's evidence for its own rails and a cohort decision never carries.
LANE_EVIDENCE_KEYS: frozenset[str] = frozenset(
    {*ContextStamp(0, 0).to_evidence(), "held_zone", "held_reason", "held_certificate",
     "ref_codes"})


def decision_evidence(evidence: Mapping[str, Any] | None) -> bool:
    """Whether a row carries evidence of the decision's own (E61's veto names its two units)."""
    return any(key not in LANE_EVIDENCE_KEYS for key in (evidence or {}))


def arrival_order(ds: Dataset, shuffle_seed: int | None = None) -> list[int]:
    """Listing ids in `first_seen_at` order (ties on the id); a seed shuffles within a day."""
    rows = sorted((listing.first_seen_at or "", listing.id) for listing in ds.listings.values())
    if shuffle_seed is None:
        return [listing_id for _stamp, listing_id in rows]
    rng = random.Random(shuffle_seed)
    out: list[int] = []
    day, bucket = "", []
    for stamp, listing_id in rows:
        if stamp[:10] != day:
            rng.shuffle(bucket)
            out.extend(bucket)
            bucket, day = [], stamp[:10]
        bucket.append(listing_id)
    rng.shuffle(bucket)
    return out + bucket


def cohort_pass(
    ds: Dataset, settings: Settings, model: LogisticModel,
    must_link: frozenset[tuple[int, int]] = frozenset(),
    must_not_link: frozenset[tuple[int, int]] = frozenset(),
) -> tuple[dict[tuple[int, int], dict[str, Any]], dict[int, list[int]]]:
    """`(pairs, clusters)`: each pair's zone, score, reason, certificate, veto, families and
    probes, and the groups, over the whole cohort at once."""
    fps = build_all(ds, settings)
    pairs, _stats = generate_pairs(fps, settings)
    ctx = FeatureContext.build(fps, settings, ds)
    ctx.index_attrs(fps, ds.listings)
    hazard = ContextIndex.build(ds.listings, ds.images_by_listing)
    decisions, vetoed = [], set()
    out: dict[tuple[int, int], dict[str, Any]] = {}
    slots: dict[tuple[int, int], dict[str, tuple[float, bool]]] = {}
    for lo, hi in sorted(pairs):
        feats = pair_features(fps[lo], fps[hi], ds.listings[lo], ds.listings[hi],
                              ds.images(lo), ds.images(hi), ctx, settings)
        decision = decide_pair(fps[lo], fps[hi], ds.listings[lo], ds.listings[hi], feats,
                               pairs[(lo, hi)], model, settings, hazard)
        decisions.append(decision)
        if decision.veto == UNIT_DESIGNATOR_VETO:
            vetoed.add((lo, hi))
        if storable({"zone": decision.zone, "score": decision.score,
                     "evidence": decision.evidence}, settings.store_floor):
            slots[(lo, hi)] = {name: feats[name] for name in FEATURE_SLOTS if name in feats}
        out[(lo, hi)] = {
            "zone": decision.zone, "score": round(float(decision.score), 9),
            "reason": decision.reason, "certificate": decision.certificate,
            "veto": decision.veto, "families": sorted(decision.families),
            "probes": sorted(pairs[(lo, hi)]),
            "evidence": decision_evidence(decision.evidence),
        }
    clustered = cluster_pairs(decisions, ds.listings, fps, settings, must_not_link,
                              relation_for(settings, ds.listings, slots),
                              must_link=must_link, machine_vetoes=frozenset(vetoed))
    return out, {key: list(members) for key, members in clustered.clusters.items()}
