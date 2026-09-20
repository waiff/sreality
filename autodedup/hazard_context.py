"""Hazard context: WHERE a false merge can happen, computed from the cohort at decision time.

W8's finding is that a band pair's risk is not a property of the pair alone but of the place and
the moment it sits in. Two facts carry almost all of it:

1. **Co-liveness.** Two adverts that were never live at the same time cannot be two units a
   developer was selling side by side; one of them had already gone when the other appeared.
   Two adverts that WERE live together at one address, near-identical, are the developer
   false-merge shape the standing ruling is about (E46 already names this; here it becomes a
   stratifier, not only a guard).
2. **The shape stack at the address.** An address block holding many listings of ONE unit shape
   is a price list, and a pair drawn from it is a coin toss between two units. W8 measured the
   four gold/operator negatives that survive strong photo evidence and a tied price: every one
   of them is in such a block or was live alongside its partner.

`live_overlap_days` exists because `structural_truth.overlap_days` ends a delisted advert at
`inactive_at`. That stamp is when the DELISTING WAS DETECTED — rule #3's completeness-gated
sweep — not when the advert was last seen: in this cohort the gap is a median 0.57 d but runs to
70 d, and 391 of 3,120 delisted listings carry a gap over a week. Reading it as the end of the
live window manufactures co-liveness for 1,492 of g5's 15,856 stored pairs. The last SIGHTING
(`last_seen_at`, rule #4) is the honest end; a still-active advert has no end yet.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Sequence

from autodedup.dataset import Listing
from autodedup.structural_truth import address_block_key

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
    return start, (_stamp(listing.last_seen_at) or _stamp(listing.inactive_at))


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
        """Many listings, almost one shape: a developer's price list, the hazard context."""
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


def is_safe_context(a: Listing, b: Listing,
                    cells: Mapping[tuple[str, str], BlockCell],
                    catalog_ratio_max: float) -> bool:
    """W8's SAFE context: the pair cannot be two units of one development sold side by side.

    Disjoint live windows, an address block that is not a one-shape stack, and image agreement
    that is not merely the developer's catalogue (E46's own threshold)."""
    cell = pair_cell(a, b, cells)
    return (disjoint_windows(a, b)
            and not (cell is not None and cell.is_shape_stack)
            and catalog_ratio_max < 0.8)
