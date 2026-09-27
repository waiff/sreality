"""The tests' whole-cohort driver over `BlockIndex` — test-only, and not a decision path.

`generate_pairs` indexes every fingerprint and asks each one for its candidates: the primitive
the lane's retrieval (`incremental.retrieve`) must reproduce exactly (E70), driven all at once so
a blocking unit test can read a cohort's pair set and its stats. `build_all` fingerprints a
whole cohort. Neither decides a pair; the one decision path is `run_pass` (SW1). They left
`autodedup/` in SW1 (review: zero production callers) — a K row records why they are kept here.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

from autodedup.blocking import BlockIndex
from autodedup.dataset import Dataset
from autodedup.fingerprint import Fingerprint, build_fingerprint
from autodedup.settings import Settings
from autodedup.stock import StockIndex


def _percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile — no interpolation, so a count stays a count."""
    if not values:
        return 0.0
    rank = max(1, min(len(values), math.ceil(q * len(values))))
    return float(values[rank - 1])


def generate_pairs(
    fps: dict[int, Fingerprint], settings: Settings
) -> tuple[dict[tuple[int, int], set[str]], dict[str, Any]]:
    """Every guard-clean candidate pair with the union of the probes that found it, plus stats."""
    index = BlockIndex(settings)
    for listing_id in sorted(fps):
        index.add(fps[listing_id])
    index.finalize()

    pairs: dict[tuple[int, int], set[str]] = {}
    counts: list[int] = []
    capped = 0
    zero = 0
    null_cat_group = 0
    zero_by_null_attr = 0
    for listing_id in sorted(fps):
        fp = fps[listing_id]
        found = index.candidates(fp)
        counts.append(len(found))
        if len(found) >= settings.max_candidates_per_listing:
            capped += 1
        # A NULL category forms its own key space in K1/K3/K6, so an unknown-category listing
        # can never block with a known one even where the rule floor would allow the pair.
        null_attr = fp.cat_group is None or fp.category_type is None
        if null_attr:
            null_cat_group += 1
        if not found:
            zero += 1
            if null_attr:
                zero_by_null_attr += 1
        for other, probes in found.items():
            lo, hi = (listing_id, other) if listing_id < other else (other, listing_id)
            known = pairs.get((lo, hi))
            if known is not None:
                known |= probes
                continue
            pairs[(lo, hi)] = set(probes)

    counts.sort()
    per_probe: dict[str, int] = {probe: 0 for probe in index.probes}
    for probes in pairs.values():
        for probe in probes:
            per_probe[probe] += 1
    stats: dict[str, Any] = {
        "n_listings": len(fps),
        "n_pairs": len(pairs),
        "candidates_per_listing": {
            "p50": _percentile(counts, 0.50),
            "p90": _percentile(counts, 0.90),
            "p99": _percentile(counts, 0.99),
            "mean": (sum(counts) / len(counts)) if counts else 0.0,
            "max": float(counts[-1]) if counts else 0.0,
        },
        "listings_with_zero_candidates": zero,
        "listings_at_cap": capped,
        "pairs_per_probe": per_probe,
        "exploded_keys_per_probe": {
            probe: len(keys) for probe, keys in index.exploded.items()
        },
        "keys_per_probe": {probe: len(buckets) for probe, buckets in index.postings.items()},
        # The largest surviving bucket per probe: K6 keys on `country_status`, a constant for
        # every foreign row, so its bucket size is the number to watch before production.
        "largest_bucket_per_probe": {
            probe: max((len(members) for members in buckets.values()), default=0)
            for probe, buckets in index.postings.items()
        },
        "listings_with_null_cat_group": null_cat_group,
        "zero_candidate_listings_by_null_attr": zero_by_null_attr,
        "guarded_pairs": dict(sorted(index.veto_counts.items())),
        "n_guarded_pairs": sum(index.veto_counts.values()),
    }
    return pairs, stats


def build_index(fps: Iterable[Fingerprint], settings: Settings) -> BlockIndex:
    """A finalized index over the given fingerprints — the probe half, without pairing."""
    index = BlockIndex(settings)
    for fp in sorted(fps, key=lambda item: item.listing_id):
        index.add(fp)
    return index.finalize()


def build_all(ds: Dataset, settings: Settings) -> dict[int, Fingerprint]:
    """Fingerprints for the whole cohort, in listing-id order so every pass is reproducible."""
    stock = StockIndex.of_dataset(ds, settings)
    return {
        listing_id: build_fingerprint(
            ds.listings[listing_id], ds.images(listing_id), settings, stock
        )
        for listing_id in sorted(ds.listings)
    }
