"""Hazard context: the census a merge sits in, and the rail that census owes (E63/E64).

W8a's premise was that a false merge lives in a CONTEXT — a dense development — and that
outside it the engine could merge at a lower bar. W8's verification refuted the strong form
and this module is what survived it, with the refutations kept in place rather than deleted:

* **The one-shape-stack test is INERT as a warrant clause.** Removing it from the proposed
  safe-context rule moved 60 more band pairs with the identical negatives, and all 23 labelled
  pairs it withheld were duplicates. It stays here as a REPORTED signal (`is_shape_stack`) and
  is never read by the rule floor.
* **Development density is not the hazard.** The cohort's densest block (`ruian:21778370`,
  51 listings) contributes 31 evidence-passing pairs and every one is a correct merge, while
  the actual leak is a 60-listing coworking product catalogue that a density test passes.
  So the census enters the decision only in the REFUSING direction (`fungible_catalogue`),
  never to widen a cut.
* **A census is retrospective.** 84.7 % of band cells grew after the moment the pair was first
  decidable (median 1.33x, p90 2.71x, max 50.5x) and an image's phash population only ever
  rises, so both signals are monotone in the UNSAFE direction: a merge taken under a small
  census may be one today's census would refuse. Any settings row that switches a census limb
  on therefore owes the rail (`rail_reopen`), which demotes such a merge back to the band —
  never a direct unmerge, which belongs to the operator's chokepoint.

`live_overlap_days` reads the honest clock (`dataset.live_end_stamp`, E62) unconditionally:
these signals guard a decision, and a guard may not be wrong to spare another guard.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Sequence

from autodedup.dataset import Image, Listing, live_end_stamp
from autodedup.text_facts import address_block_key, states_from_price

_FAR_FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)

# Two listings count as the same unit SHAPE when the disposition matches and the areas are
# within this much: a price list repeats one shape, a mixed building does not.
SHAPE_AREA_REL_TOL: float = 0.01
# ...and as confusable TWINS at one address at this tolerance, which is E5's supporting band.
TWIN_AREA_REL_TOL: float = 0.03
# A block that stacks this many listings into this few shapes is a price list, not a building.
STACK_MIN_LISTINGS: int = 5
STACK_MAX_SHAPES: int = 2


def _stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def live_window(listing: Listing) -> tuple[datetime | None, datetime | None]:
    """(first sighting, last sighting). An active advert has no end yet, so it reads open."""
    start = _stamp(listing.first_seen_at)
    if listing.is_active:
        return start, _FAR_FUTURE
    return start, _stamp(live_end_stamp(listing))


def live_overlap_days(a: Listing, b: Listing) -> float:
    """Days both adverts were demonstrably live at once, measured on SIGHTINGS."""
    start_a, end_a = live_window(a)
    start_b, end_b = live_window(b)
    if None in (start_a, start_b, end_a, end_b):
        return 0.0
    span = min(end_a, end_b) - max(start_a, start_b)  # type: ignore[operator]
    return max(0.0, span / timedelta(days=1))


def disjoint_windows(a: Listing, b: Listing) -> bool:
    return live_overlap_days(a, b) <= 0.0


def category_group(listing: Listing) -> str:
    return f"{listing.category_main or '?'}|{listing.category_type or '?'}"


def _close(x: float | None, y: float | None, tol: float) -> bool:
    if x is None or y is None:
        return True
    scale = max(abs(x), abs(y))
    return scale == 0 or abs(x - y) <= tol * scale


@dataclass(slots=True, frozen=True)
class BlockCell:
    """One address block inside one deal+category group — the unit a merge could confuse."""

    key: str
    category_group: str
    n_listings: int
    n_unit_shapes: int
    n_brokers: int
    n_source_native_ids: int

    @property
    def is_shape_stack(self) -> bool:
        """Many listings, almost one shape. REPORTED ONLY — W8 measured this clause inert as a
        warrant term (it withheld 23 labelled duplicates and 0 labelled negatives), so no rule
        floor reads it."""
        return (self.n_listings >= STACK_MIN_LISTINGS
                and self.n_unit_shapes <= STACK_MAX_SHAPES)


def _shape_count(listings: Sequence[Listing]) -> int:
    kept: list[tuple[str | None, float | None]] = []
    for listing in listings:
        shape = (listing.disposition, listing.area_m2)
        if not any(shape[0] == other[0] and _close(shape[1], other[1], SHAPE_AREA_REL_TOL)
                   for other in kept):
            kept.append(shape)
    return len(kept)


def block_cells(listings: Iterable[Listing]) -> dict[tuple[str, str], BlockCell]:
    """Census of every (address block, category group) cell the cohort holds."""
    members: dict[tuple[str, str], list[Listing]] = defaultdict(list)
    for listing in listings:
        members[(address_block_key(listing), category_group(listing))].append(listing)
    out: dict[tuple[str, str], BlockCell] = {}
    for (key, group), rows in members.items():
        out[(key, group)] = BlockCell(
            key=key,
            category_group=group,
            n_listings=len(rows),
            n_unit_shapes=_shape_count(rows),
            n_brokers=len({r.broker_key for r in rows if r.broker_key}),
            n_source_native_ids=len({r.source_id_native for r in rows if r.source_id_native}),
        )
    return out


def cell_for(listing: Listing, cells: Mapping[tuple[str, str], BlockCell]) -> BlockCell | None:
    return cells.get((address_block_key(listing), category_group(listing)))


def pair_cell(a: Listing, b: Listing,
              cells: Mapping[tuple[str, str], BlockCell]) -> BlockCell | None:
    """The WIDER of the two sides' cells.

    A pair whose two adverts resolve to different blocks, or disagree on the category group
    because one side's column is NULL, must fail towards the stricter side — the same direction
    E48 takes when a stratum's side is unknown."""
    candidates = [c for c in (cell_for(a, cells), cell_for(b, cells)) if c is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.n_listings)


def confusable_twins(listing: Listing, block_members: Sequence[Listing]) -> list[int]:
    """Other listings at this address a merge could fuse this one with: compatible deal type,
    same disposition, area within E5's supporting band."""
    out: list[int] = []
    for other in block_members:
        if other.id == listing.id:
            continue
        if (listing.category_type and other.category_type
                and listing.category_type != other.category_type):
            continue
        if listing.disposition and other.disposition and listing.disposition != other.disposition:
            continue
        if not _close(listing.area_m2, other.area_m2, TWIN_AREA_REL_TOL):
            continue
        out.append(other.id)
    return out


def block_members(listings: Iterable[Listing]) -> dict[str, list[Listing]]:
    out: dict[str, list[Listing]] = defaultdict(list)
    for listing in listings:
        out[address_block_key(listing)].append(listing)
    return out


@dataclass(slots=True, frozen=True)
class ContextStamp:
    """The census a promotion was taken under — the rail's only input besides today's census."""

    cell_n_listings: int
    shared_image_pop_min: int

    def to_evidence(self) -> dict[str, str]:
        return {
            "context_cell_n": str(self.cell_n_listings),
            "context_image_pop_min": str(self.shared_image_pop_min),
        }

    @classmethod
    def from_evidence(cls, evidence: Mapping[str, str]) -> "ContextStamp | None":
        try:
            return cls(int(evidence["context_cell_n"]), int(evidence["context_image_pop_min"]))
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(slots=True, frozen=True)
class PairContext:
    """One pair's census, decoupled from the index that produced it.

    A stored run row carries this so a re-simulation replays the census the decision was taken
    under instead of guessing at today's — the same discipline K-B's window stub follows."""

    stamp: ContextStamp
    from_price: bool

    def to_json(self) -> dict[str, object]:
        return {
            "cell_n": self.stamp.cell_n_listings,
            "image_pop": self.stamp.shared_image_pop_min,
            "from_price": self.from_price,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, object] | None) -> "PairContext | None":
        if not raw:
            return None
        try:
            return cls(
                ContextStamp(int(raw["cell_n"]), int(raw["image_pop"])),  # type: ignore[arg-type]
                bool(raw.get("from_price")),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(slots=True, frozen=True)
class ContextIndex:
    """Everything the refusing direction needs, computed once per cohort.

    `phash_population` is the CORPUS-wide carrier count the export stamps on each image, not a
    count within the pair: an image ten listings carry is stock whether or not this pair is one
    of the ten."""

    cells: Mapping[tuple[str, str], BlockCell]
    listing_phashes: Mapping[int, frozenset[int]]
    phash_population: Mapping[int, int]
    from_price: frozenset[int]

    @classmethod
    def build(cls, listings: Mapping[int, Listing],
              images_by_listing: Mapping[int, Sequence[Image]]) -> "ContextIndex":
        phashes: dict[int, frozenset[int]] = {}
        population: dict[int, int] = {}
        for listing_id, images in images_by_listing.items():
            seen = {image.phash for image in images if image.phash is not None}
            if seen:
                phashes[listing_id] = frozenset(seen)
            for image in images:
                if image.phash is None or image.pop is None:
                    continue
                population[image.phash] = max(population.get(image.phash, 0), int(image.pop))
        return cls(
            cells=block_cells(listings.values()),
            listing_phashes=phashes,
            phash_population=population,
            from_price=frozenset(
                listing_id for listing_id, listing in listings.items()
                if states_from_price(listing.description)
            ),
        )

    def cell_size(self, a: Listing, b: Listing) -> int:
        cell = pair_cell(a, b, self.cells)
        return cell.n_listings if cell is not None else 0

    def shared_image_population(self, lo: int, hi: int) -> int:
        """The LEAST-carried image the two adverts share, or 0 when they share none.

        The least, not the most: the question this answers is whether the pair's whole image
        agreement is stock, which is the Spaces-coworking failure shape — a warrant resting on
        three photos the chain prints on every advert it runs. Reading the MOST-carried image
        instead was measured on g5 and would refuse 101 promotions carrying 35 labelled
        DUPLICATES, punishing a pair that shares twelve private photographs and one templated
        floor plan. `0` when nothing is shared, which no limb can reach."""
        shared = self.listing_phashes.get(lo, frozenset()) & self.listing_phashes.get(
            hi, frozenset()
        )
        return min((self.phash_population.get(value, 0) for value in shared), default=0)

    def stamp(self, a: Listing, b: Listing) -> ContextStamp:
        lo, hi = (a.id, b.id) if a.id < b.id else (b.id, a.id)
        return ContextStamp(self.cell_size(a, b), self.shared_image_population(lo, hi))

    def pair_context(self, a: Listing, b: Listing) -> PairContext:
        return PairContext(
            self.stamp(a, b), a.id in self.from_price or b.id in self.from_price
        )


def fungible_catalogue(
    stamp: ContextStamp,
    from_price: bool,
    *,
    block_min: int | None,
    image_population_min: int | None,
    from_price_veto: bool,
) -> str | None:
    """C3: the name of the fungible-catalogue limb that refuses this promotion, or None.

    Fungible inventory — a coworking chain's desks, a developer's price list — is the shape
    that defeats every resemblance rule, because two adverts really are interchangeable
    descriptions of interchangeable things. Each limb is its own settings row because each one
    costs differently: the FROM price and an all-stock warrant are free on the g5 cohort, while
    the block-size limb withholds 66 labelled duplicates and is therefore off by default."""
    if from_price_veto and from_price:
        return "from_price"
    if block_min is not None and stamp.cell_n_listings >= block_min:
        return "catalogue_block"
    if image_population_min is not None and stamp.shared_image_pop_min >= image_population_min:
        return "stock_images"
    return None


def rail_reopen(
    stamped: ContextStamp,
    current: ContextStamp,
    *,
    block_min: int | None,
    image_population_min: int | None,
) -> str | None:
    """C4: which census threshold this merge's context has crossed SINCE it was taken.

    A census answer only ever gets worse — a block grows, an image gains carriers — so the
    question is one-sided: was the pair under the limb's bar when it merged and over it now?
    The answer is advisory; what to DO about it is `rail_plan`'s business."""
    if (block_min is not None
            and stamped.cell_n_listings < block_min <= current.cell_n_listings):
        return "catalogue_block"
    if (image_population_min is not None
            and stamped.shared_image_pop_min < image_population_min <= current.shared_image_pop_min):
        return "stock_images"
    return None


@dataclass(slots=True, frozen=True)
class RailAction:
    lo: int
    hi: int
    limb: str
    block: str
    stamped: ContextStamp
    current: ContextStamp

    def to_json(self) -> dict[str, object]:
        return {
            "lo": self.lo, "hi": self.hi, "limb": self.limb, "block": self.block,
            "stamped": [self.stamped.cell_n_listings, self.stamped.shared_image_pop_min],
            "current": [self.current.cell_n_listings, self.current.shared_image_pop_min],
        }


def rail_plan(
    merges: Iterable[tuple[int, int, ContextStamp, str]],
    index: ContextIndex,
    listings: Mapping[int, Listing],
    *,
    block_min: int | None,
    image_population_min: int | None,
    max_per_block: int,
    exempt: frozenset[tuple[int, int]] = frozenset(),
) -> tuple[list[RailAction], dict[str, int]]:
    """Every stamped promotion the census has overtaken, capped per block.

    The cap exists because a block that doubles overnight would otherwise re-open every merge
    in it at once; the deferred ones stay merged and are named in the counters, so a capped
    block is visible rather than silently skipped. Certificates are exempt by construction —
    the caller passes them in `exempt` — because a shared order code or a disjoint-window
    re-post is a per-pair FACT that no later census can decay."""
    actions: list[RailAction] = []
    deferred: dict[str, int] = {}
    per_block: dict[str, int] = {}
    for lo, hi, stamp, block in sorted(merges):
        if (lo, hi) in exempt:
            continue
        a, b = listings.get(lo), listings.get(hi)
        if a is None or b is None:
            continue
        current = index.stamp(a, b)
        limb = rail_reopen(stamp, current, block_min=block_min,
                           image_population_min=image_population_min)
        if limb is None:
            continue
        if per_block.get(block, 0) >= max_per_block:
            deferred[block] = deferred.get(block, 0) + 1
            continue
        per_block[block] = per_block.get(block, 0) + 1
        actions.append(RailAction(lo, hi, limb, block, stamp, current))
    counters = {
        "reopened": len(actions),
        "deferred_by_cap": sum(deferred.values()),
        "blocks_at_cap": len(deferred),
        "blocks_touched": len(per_block),
    }
    return actions, counters
