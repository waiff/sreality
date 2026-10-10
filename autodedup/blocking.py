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
E936 adds the eighth under the same dial, `quarter`: the same key at a split city's quarter, for
the ads that do not probe the town key, so one whose two home keys explode is not dark.
"""

from __future__ import annotations

from typing import Any

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
# The town and quarter probes are the WEAKEST evidence classes, so they fill after every other
# probe and the fan-out cap (E17) can only ever discard them, never a candidate today's probes
# reach.
TOWN_PROBE: str = "town"
QUARTER_PROBE: str = "quarter"


def probe_priority(settings: Settings) -> tuple[str, ...]:
    """The fill order this settings row blocks with: E300 appends the town probe, E936 the
    quarter probe after it."""
    if not settings.attr_probe_town_grain:
        return PROBE_PRIORITY
    return PROBE_PRIORITY + (TOWN_PROBE, QUARTER_PROBE)


def probes_town_key(fp: Fingerprint) -> bool:
    """E300: an advert with a known quarter of a split city stays inside its quarter; every
    other advert — any advert outside the three cities, an unknown-quarter one inside — reaches
    the whole town. Everyone with a town key POSTS it, so an unknown-quarter advert meets every
    quarter of its city, and the listings that probe a key are always among those posted
    under it (what the real-time lane's neighbourhood re-probe relies on, E71)."""
    return not (fp.obec_kod in SPLIT_CITY_OBEC_KODS and fp.cast_obce_kod is not None)


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
        # E936: an ad that does not probe the town key keys the same at its quarter.
        if self.settings.attr_probe_town_grain and fp.area_band is not None:
            composite = (fp.cat_group, fp.category_type, fp.disposition, fp.area_band)
            if fp.obec_kod is not None:
                out.append((TOWN_PROBE, (fp.obec_kod, *composite)))
            if not probes_town_key(fp):
                out.append((QUARTER_PROBE, (fp.block_key, *composite)))
        return out

    def probe_keys(self, fp: Fingerprint) -> list[tuple[str, Any]]:
        """The keys this listing LOOKS UP — the ±1 neighbourhood on band and decile."""
        out: list[tuple[str, Any]] = []
        for probe, key in self.index_keys(fp):
            if probe == "attr_area":
                block_key, cat_group, category_type, band = key
                for offset in (-1, 0, 1):
                    out.append((probe, (block_key, cat_group, category_type, band + offset)))
            elif probe in (TOWN_PROBE, QUARTER_PROBE):
                if probe == TOWN_PROBE and not probes_town_key(fp):
                    continue
                grain, cat_group, category_type, disposition, band = key
                for offset in (-1, 0, 1):
                    out.append((probe, (grain, cat_group, category_type, disposition,
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
