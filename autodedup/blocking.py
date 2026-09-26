"""Candidate generation — six optional probes over one row per listing (PROGRAM.md E14-E17).

Index probes, never a corpus join: each listing writes a handful of keys, and retrieval is a
lookup of that listing's own keys, the union of the co-members, minus self, capped.

Two asymmetries are deliberate. A listing is INDEXED under its exact area band and price
decile but PROBES the ±1 neighbourhood, so a ±1 tolerance costs three lookups instead of
widening every bucket (indexing all three would silently pair bands two apart). And K4/K5
carry no location at all (E15): the engine's reach must not be capped by the location
program's 10% geo-blockable share.

Hard guards (E2-E5) are applied INSIDE retrieval, before the fan-out cap spends a slot: a
candidate the rule floor will veto anyway must not displace the one true duplicate (E17 says
the cap can only discard the weakest evidence class). Every refusal is counted once per pair,
by the rule that refused it, so the blocking stats still show what the rule floor removed.

E300 (W30) adds a seventh, the `town` probe, behind `attr_probe_town_grain`: the attribute
probes key on the resolver's finest grain (a cast obce where one was found), so an advert
located only to the town never met one located to a cast. The home keys are left as they are;
the town probe is appended after them, keyed town + disposition + area band (path C's C1).
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

from autodedup.fingerprint import Fingerprint
from autodedup.guards import pair_veto
from autodedup.settings import Settings

# Fan-out fills in this order (E17), so the cap can only ever discard the weakest class.
PROBE_PRIORITY: tuple[str, ...] = (
    "addr", "phash", "text", "broker", "attr_dispo", "attr_area", "foreign",
)

FOREIGN_STATUS: str = "foreign"
_DECILES: int = 10

# E300 (W30): the operator's location grain (docs/design/new-dedup/PROGRAM.md, 2026-09-10 (d)):
# quarters split the town ONLY in Praha, Brno and Ostrava; elsewhere the grain is the town.
SPLIT_CITY_OBEC_KODS: frozenset[int] = frozenset({554782, 582786, 554821})
# The town probe is the WEAKEST evidence class, so it fills after every other probe and the
# fan-out cap (E17) can only ever discard it, never a candidate today's probes reach.
TOWN_PROBE: str = "town"


def probe_priority(settings: Settings) -> tuple[str, ...]:
    """The fill order this settings row blocks with: E300 appends the town probe."""
    return PROBE_PRIORITY + (TOWN_PROBE,) if settings.attr_probe_town_grain else PROBE_PRIORITY


def probes_town_key(fp: Fingerprint) -> bool:
    """E300: an advert with a known quarter of a split city stays inside its quarter; every
    other advert — any advert outside the three cities, an unknown-quarter one inside — reaches
    the whole town. Everyone with a town key POSTS it, so an unknown-quarter advert meets every
    quarter of its city, and the listings that probe a key are always among those posted
    under it (what the real-time lane's neighbourhood re-probe relies on, E71)."""
    return not (fp.obec_kod in SPLIT_CITY_OBEC_KODS and fp.cast_obce_kod is not None)


def _percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile — no interpolation, so a count stays a count."""
    if not values:
        return 0.0
    rank = max(1, min(len(values), math.ceil(q * len(values))))
    return float(values[rank - 1])


class BlockIndex:
    """Builds the probe posting lists over a set of fingerprints, then answers lookups."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.probes: tuple[str, ...] = probe_priority(settings)
        self.fingerprints: dict[int, Fingerprint] = {}
        self.postings: dict[str, dict[Any, list[int]]] = {probe: {} for probe in self.probes}
        self.exploded: dict[str, set[Any]] = {probe: set() for probe in self.probes}
        self.price_cuts: dict[tuple[str | None, str | None], list[float]] = {}
        self.vetoed_pairs: set[tuple[int, int]] = set()
        self.veto_counts: dict[str, int] = {}
        self._finalized = False
        self._keys_ready = False

    def add(self, fp: Fingerprint) -> None:
        """Hold the fingerprint; keys are built in `finalize`, which needs the price deciles."""
        if self._finalized:
            raise RuntimeError("BlockIndex.add after finalize")
        self.fingerprints[fp.listing_id] = fp

    def finalize(self) -> "BlockIndex":
        """Compute price deciles per (cat_group, category_type), post every index key, and
        mark any key over `max_block_size` as exploded."""
        if self._finalized:
            return self
        self._build_price_cuts()
        self._keys_ready = True
        for listing_id in sorted(self.fingerprints):
            fp = self.fingerprints[listing_id]
            for probe, key in self.index_keys(fp):
                self.postings[probe].setdefault(key, []).append(listing_id)
        for probe, buckets in self.postings.items():
            limit = self.settings.max_block_size
            self.exploded[probe] = {key for key, members in buckets.items() if len(members) > limit}
        self._finalized = True
        return self

    def _build_price_cuts(self) -> None:
        groups: dict[tuple[str | None, str | None], list[float]] = {}
        for fp in self.fingerprints.values():
            if fp.price is not None:
                groups.setdefault((fp.cat_group, fp.category_type), []).append(float(fp.price))
        for key, prices in groups.items():
            prices.sort()
            n = len(prices)
            self.price_cuts[key] = [
                prices[min(n - 1, (n * step) // _DECILES)] for step in range(1, _DECILES)
            ]

    def price_decile(self, fp: Fingerprint) -> int | None:
        """0..9 within the listing's (cat_group, category_type) cohort; None without a price,
        and None for a cohort this index never saw — an unknown decile, not the cheapest one."""
        if not self._keys_ready:
            raise RuntimeError("BlockIndex.price_decile before finalize")
        if fp.price is None:
            return None
        cuts = self.price_cuts.get((fp.cat_group, fp.category_type))
        if not cuts:
            return None
        decile = 0
        for cut in cuts:
            if float(fp.price) < cut:
                break
            decile += 1
        return min(decile, _DECILES - 1)

    def index_keys(self, fp: Fingerprint) -> list[tuple[str, Any]]:
        """The keys this listing is POSTED under — exact band, exact decile."""
        if not self._keys_ready:
            raise RuntimeError("BlockIndex.index_keys before finalize")
        out: list[tuple[str, Any]] = []
        if fp.obec_kod is not None and fp.street_key and fp.house_number:
            out.append(("addr", (fp.obec_kod, fp.street_key, fp.house_number)))
        for band_no, band_val, _phash in fp.anchor_bands:
            out.append(("phash", (band_no, band_val)))
        if fp.has_text:
            for band_no, band_val in fp.desc_bands:
                out.append(("text", (band_no, band_val)))
        if fp.broker_key:
            out.append(("broker", (fp.broker_key, fp.cat_group, fp.category_type,
                                   self.price_decile(fp))))
        if fp.block_key and fp.disposition:
            out.append(("attr_dispo", (fp.block_key, fp.cat_group, fp.category_type,
                                       fp.disposition)))
        if fp.block_key and fp.area_band is not None:
            out.append(("attr_area", (fp.block_key, fp.cat_group, fp.category_type, fp.area_band)))
        if fp.country_status == FOREIGN_STATUS and fp.area_band is not None:
            out.append(("foreign", (fp.country_status, fp.cat_group, fp.category_type,
                                    fp.area_band)))
        # E300: path C's C1 — town + disposition + area band. An advert with no area states
        # nothing the key could bound; the disposition may be unknown and then keys as such.
        if (self.settings.attr_probe_town_grain and fp.obec_kod is not None
                and fp.area_band is not None):
            out.append((TOWN_PROBE, (fp.obec_kod, fp.cat_group, fp.category_type,
                                     fp.disposition, fp.area_band)))
        return out

    def probe_keys(self, fp: Fingerprint) -> list[tuple[str, Any]]:
        """The keys this listing LOOKS UP — the ±1 neighbourhood on band and decile."""
        out: list[tuple[str, Any]] = []
        for probe, key in self.index_keys(fp):
            if probe == "attr_area":
                block_key, cat_group, category_type, band = key
                for offset in (-1, 0, 1):
                    out.append((probe, (block_key, cat_group, category_type, band + offset)))
            elif probe == TOWN_PROBE:
                if not probes_town_key(fp):
                    continue
                obec_kod, cat_group, category_type, disposition, band = key
                for offset in (-1, 0, 1):
                    out.append((probe, (obec_kod, cat_group, category_type, disposition,
                                        band + offset)))
            elif probe == "broker" and key[3] is not None:
                broker_key, cat_group, category_type, decile = key
                for offset in (-1, 0, 1):
                    neighbour = decile + offset
                    if 0 <= neighbour < _DECILES:
                        out.append((probe, (broker_key, cat_group, category_type, neighbour)))
            else:
                out.append((probe, key))
        return out

    def exploded_probes(self, fp: Fingerprint) -> set[str]:
        """Probes on which this listing sits inside an exploded key — a negative feature (E17)."""
        return {probe for probe, key in self.index_keys(fp) if key in self.exploded[probe]}

    def _guarded(self, fp: Fingerprint, other: int) -> bool:
        """True when the rule floor refuses this pair; counted once, however often it is seen."""
        pair = (fp.listing_id, other) if fp.listing_id < other else (other, fp.listing_id)
        if pair in self.vetoed_pairs:
            return True
        veto = pair_veto(fp, self.fingerprints[other], self.settings)
        if veto is None:
            return False
        self.vetoed_pairs.add(pair)
        self.veto_counts[veto] = self.veto_counts.get(veto, 0) + 1
        return True

    def candidates(self, fp: Fingerprint) -> dict[int, set[str]]:
        """Other listing ids -> the probes that reached them, filled in priority order.

        Guard-dead candidates are dropped before the cap sees them (E2-E5 ahead of E17)."""
        if not self._finalized:
            raise RuntimeError("BlockIndex.candidates before finalize")
        by_probe: dict[str, list[tuple[str, Any]]] = {probe: [] for probe in self.probes}
        for probe, key in self.probe_keys(fp):
            by_probe[probe].append((probe, key))
        out: dict[int, set[str]] = {}
        cap = self.settings.max_candidates_per_listing
        for probe in self.probes:
            for _, key in by_probe[probe]:
                if key in self.exploded[probe]:
                    continue
                for other in self.postings[probe].get(key, ()):
                    if other == fp.listing_id:
                        continue
                    hit = out.get(other)
                    if hit is not None:
                        hit.add(probe)
                    elif len(out) < cap and not self._guarded(fp, other):
                        out[other] = {probe}
        return out


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
