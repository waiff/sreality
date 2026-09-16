"""Layer 1 of the decision model (PROGRAM.md §6): the rule floor, E2-E5 and E34.

Absolute authority, no training, evaluated before anything learned and never overridden. A
guard returns the NAME of the rule it broke, not a boolean, so every refusal is reportable:
the blocking stats count vetoes by name and the cluster pass records the invariant that
refused a union.

Guards read the same fields the production chokepoint reads (E4), and they are the only
place area tolerance is defined — `area_rel_diff` is `|a-b| / max(a,b)`, i.e. the shortfall
against the LARGER side, so the relation is symmetric, bounded in [0,1) and never flatters a
pair by dividing by the smaller number.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Sequence

from autodedup.settings import Settings
from toolkit.room_taxonomy import category_main_compatible

if TYPE_CHECKING:  # pragma: no cover
    from autodedup.fingerprint import Fingerprint

LAND_CATEGORY: str = "pozemek"
FLAT_CATEGORY: str = "byt"


class GuardSide(Protocol):
    """What a guard needs of a side — satisfied by both `Fingerprint` and `Listing`."""

    category_main: str | None
    category_type: str | None
    area_m2: float | None
    disposition: str | None
    floor: int | None


def area_rel_diff(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or a <= 0.0 or b <= 0.0:
        return None
    return abs(a - b) / max(a, b)


def area_relation(
    a: float | None, b: float | None, settings: Settings | None = None
) -> str:
    """E5's three-way area verdict: `reject` >8%, `band` 3-8%, `support` <=3%, else `unknown`."""
    cfg = settings or Settings()
    diff = area_rel_diff(a, b)
    if diff is None:
        return "unknown"
    if diff > cfg.area_reject_pct:
        return "reject"
    if diff > cfg.area_band_pct:
        return "band"
    return "support"


def pair_veto(a: GuardSide, b: GuardSide, settings: Settings | None = None) -> str | None:
    """The name of the first hard guard the pair breaks (E2-E5), or None when it passes.

    Missing data is never a mismatch (E12): every clause needs BOTH sides known."""
    cfg = settings or Settings()
    if (a.category_type is not None and b.category_type is not None
            and a.category_type != b.category_type):
        return "category_type"
    if not category_main_compatible(a.category_main, b.category_main):
        return "category_main"
    if area_relation(a.area_m2, b.area_m2, cfg) == "reject":
        return "area"
    is_land = LAND_CATEGORY in (a.category_main, b.category_main)
    if (not is_land and a.disposition is not None and b.disposition is not None
            and a.disposition != b.disposition):
        return "disposition"
    if (a.category_main == FLAT_CATEGORY and b.category_main == FLAT_CATEGORY
            and a.floor is not None and b.floor is not None
            and abs(a.floor - b.floor) >= 2):
        return "floor"
    return None


def floor_relation(a: GuardSide, b: GuardSide) -> str:
    """`reject` / `band` (delta 1 - portals disagree on přízemí) / `support` / `unknown`."""
    if (a.category_main != FLAT_CATEGORY or b.category_main != FLAT_CATEGORY
            or a.floor is None or b.floor is None):
        return "unknown"
    delta = abs(a.floor - b.floor)
    if delta >= 2:
        return "reject"
    return "band" if delta == 1 else "support"


def cluster_invariants_ok(
    members: Sequence["Fingerprint"],
    settings: Settings | None = None,
    must_not_link: frozenset[tuple[int, int]] | set[tuple[int, int]] = frozenset(),
) -> str | None:
    """E34 on the MERGED member set: the violated invariant's name, or None when it holds."""
    cfg = settings or Settings()
    if len(members) > cfg.max_cluster_size:
        return "size"

    types = {fp.category_type for fp in members if fp.category_type is not None}
    if len(types) > 1:
        return "category_type"

    cats = sorted({fp.category_main for fp in members if fp.category_main is not None})
    for index, left in enumerate(cats):
        for right in cats[index + 1:]:
            if not category_main_compatible(left, right):
                return "compat_class"

    areas = [fp.area_m2 for fp in members if fp.area_m2 is not None and fp.area_m2 > 0.0]
    if areas and (max(areas) - min(areas)) / max(areas) > cfg.cluster_area_spread:
        return "area_spread"

    is_land = any(fp.category_main == LAND_CATEGORY for fp in members)
    if not is_land:
        dispositions = {fp.disposition for fp in members if fp.disposition is not None}
        if len(dispositions) > 1:
            return "disposition"

    floors = [fp.floor for fp in members
              if fp.category_main == FLAT_CATEGORY and fp.floor is not None]
    if floors and max(floors) != min(floors):
        return "floor_spread"

    ids = [fp.listing_id for fp in members]
    for index, left in enumerate(ids):
        for right in ids[index + 1:]:
            pair = (left, right) if left < right else (right, left)
            if pair in must_not_link:
                return "must_not_link"
    return None
