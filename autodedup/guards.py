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

from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol, Sequence

from autodedup.dataset import Listing
from autodedup.settings import Settings
from autodedup.text_facts import address_block_key, unit_designators
from toolkit.room_taxonomy import category_main_compatible

if TYPE_CHECKING:  # pragma: no cover
    from autodedup.d43 import ClusterRelation
    from autodedup.fingerprint import Fingerprint

LAND_CATEGORY: str = "pozemek"
FLAT_CATEGORY: str = "byt"

UNIT_DESIGNATOR_VETO: str = "unit_designator_conflict"


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


def unit_designator_conflict(
    a: Listing, b: Listing, settings: Settings | None = None
) -> tuple[str, str] | None:
    """E61: the two designators, when both adverts name a DIFFERENT unit of one address block.

    A developer's adverts inside one building are near-identical by construction — same address,
    same photographs, same template, often the same price — so no resemblance can separate them.
    The one thing that can is the building's own naming: `byt (č.3)`, `označením B36`,
    `jednotka č. 12`. When both bodies name exactly one unit and the names differ at one address,
    they are two units and no certificate and no score may merge them.

    Exactly one designator per side is required. A body listing several (`jednotky č. 3, 5 a 7`)
    is a project's price list, not this advert's identity, and two such lists overlapping proves
    nothing either way. The block must match too: two buildings number their flats independently,
    so `byt č. 3` in one house and `byt č. 5` in another is not a conflict.

    Honest about its evidence: this fires on 3 distinct (block, unit-pair) facts in ONE Zizkov
    building in the g5 cohort (ruian:21778370, units 1/3/5). It is a rule with a sound mechanism
    measured on one development, never a rate."""
    if settings is not None and not settings.unit_designator_veto:
        return None
    left, right = unit_designators(a.description), unit_designators(b.description)
    # Read the texts FIRST: a side that names no unit ends the rule here, which is also the
    # only thing a stub side (`evaluate._SimSide`, everything None) can honestly answer.
    if len(left) != 1 or len(right) != 1 or left == right:
        return None
    if address_block_key(a) != address_block_key(b):
        return None
    return next(iter(left)), next(iter(right))


def floor_relation(a: GuardSide, b: GuardSide) -> str:
    """`reject` / `band` (delta 1 - portals disagree on přízemí) / `support` / `unknown`."""
    if (a.category_main != FLAT_CATEGORY or b.category_main != FLAT_CATEGORY
            or a.floor is None or b.floor is None):
        return "unknown"
    delta = abs(a.floor - b.floor)
    if delta >= 2:
        return "reject"
    return "band" if delta == 1 else "support"


def _spread_is_printed_one(
    members: Sequence["Fingerprint"], cfg: Settings, relation: "ClusterRelation",
    closure_of: Mapping[int, int] | None = None,
) -> bool:
    """E280 at cluster grain: every pair of members whose COLUMNS are further apart than the
    spread prints the same floor-area figures in both bodies, so the spread is the portals'."""
    from autodedup.indistinguishable import _printed_areas_prevail

    reader = getattr(relation, "listings", None)
    if reader is None:
        return False
    listings = reader()
    sized = [(fp.listing_id, float(fp.area_m2)) for fp in members
             if fp.area_m2 is not None and fp.area_m2 > 0.0]
    for index, (left_id, left) in enumerate(sized):
        for right_id, right in sized[index + 1:]:
            if abs(left - right) / max(left, right) <= cfg.cluster_area_spread:
                continue
            if _same_closure(left_id, right_id, closure_of):
                continue
            a, b = listings.get(left_id), listings.get(right_id)
            if a is None or b is None or not _printed_areas_prevail(a, b):
                return False
    return True


def _same_closure(left: int, right: int, closure_of: Mapping[int, int] | None) -> bool:
    if not closure_of:
        return False
    key = closure_of.get(left)
    return key is not None and key == closure_of.get(right)


def _apart(values: Sequence[tuple[int, Any]], closure_of: Mapping[int, int] | None,
           far: Callable[[Any, Any], bool]) -> bool:
    """Whether two stated values of DIFFERENT closures are `far` apart (E910). Without a ruling
    every advert is its own closure, and every `far` here is decided by the extreme pair (a
    relative gap, or plain inequality), so that case is the old O(n) min/max rule exactly; only
    a set holding two adverts of one closure reads pair by pair."""
    if not values:
        return False
    keys = [closure_of[i] for i, _v in values if i in closure_of] if closure_of else []
    if len(keys) == len(set(keys)):
        ordered = [v for _i, v in values]
        return far(min(ordered), max(ordered))
    return any(far(left, right)
               for index, (left_id, left) in enumerate(values)
               for right_id, right in values[index + 1:]
               if not _same_closure(left_id, right_id, closure_of))


def cluster_invariants_ok(
    members: Sequence["Fingerprint"],
    settings: Settings | None = None,
    must_not_link: frozenset[tuple[int, int]] | set[tuple[int, int]] = frozenset(),
    relation: "ClusterRelation | None" = None,
    closure_of: Mapping[int, int] | None = None,
) -> str | None:
    """E34 on the MERGED member set: the violated invariant's name, or None when it holds.

    E132: `relation` is D43 read at cluster grain — a group is built transitively, so a
    pairwise gate alone lets A-B and B-C pass while A and C differ on the floor. It is the last
    limb because it is the expensive one and it reads the two BODIES, which a fingerprint
    cannot.

    E910: `closure_of` maps a member to its must-link closure (the operator's `same` rulings,
    Decision 8), and an advert it does not name is a closure of its own. The hard limbs (size,
    deal type, category) and the operator's must-not-links read the whole set; the spreads
    (area, disposition, floor) are read across closures only, because the operator has ruled
    two adverts of one closure one property — with no ruling that is every pair, today's rule
    exactly. The relation must be bound to the same closures by the caller.
    """
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

    sized = [(fp.listing_id, fp.area_m2) for fp in members
             if fp.area_m2 is not None and fp.area_m2 > 0.0]
    if _apart(sized, closure_of,
              lambda lo, hi: abs(hi - lo) / max(lo, hi) > cfg.cluster_area_spread):
        if not (cfg.d43_printed_area_prevails and relation is not None
                and _spread_is_printed_one(members, cfg, relation, closure_of)):
            return "area_spread"

    is_land = any(fp.category_main == LAND_CATEGORY for fp in members)
    if cfg.cluster_disposition and not is_land:
        stated = [(fp.listing_id, fp.disposition) for fp in members
                  if fp.disposition is not None]
        if _apart(stated, closure_of, lambda a, b: a != b):
            return "disposition"

    if cfg.cluster_floor_spread:
        floors = [(fp.listing_id, fp.floor) for fp in members
                  if fp.category_main == FLAT_CATEGORY and fp.floor is not None]
        if _apart(floors, closure_of, lambda a, b: a != b):
            return "floor_spread"

    ids = [fp.listing_id for fp in members]
    for index, left in enumerate(ids):
        for right in ids[index + 1:]:
            pair = (left, right) if left < right else (right, left)
            if pair in must_not_link:
                return "must_not_link"

    if relation is not None and relation.violating_pair(ids) is not None:
        return "d43_distinguishable"
    return None
