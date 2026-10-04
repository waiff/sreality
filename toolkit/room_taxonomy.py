"""Single source of truth for the image room/plot tag taxonomy and its FAMILY
grouping.

Pure data — no heavy imports — so the classifiers that emit these tags and every
consumer that groups them read ONE definition. The CLIP tagger's anchor→tag collapse
lives in `data/clip_taxonomy.json`; this module groups the resulting `logical_tag`
values into families.
"""

from __future__ import annotations

# Every logical_tag the CLIP tagger / LLM classifier can emit, grouped into a FAMILY:
#   interior — a unit's own rooms.
#   exterior — facade / outdoor shots a whole development reuses across its units.
#   common   — SHARED building circulation (stairwells): every unit in a building shows the
#              same one.
#   plan     — floor / site plans; shared templates.
#   other    — unclassifiable content; treated as unknown.
ROOM_FAMILIES: dict[str, str] = {
    "kitchen": "interior",
    "bathroom": "interior",
    "toilet": "interior",
    "living_room": "interior",
    "bedroom": "interior",
    "hallway": "interior",
    "exterior_facade": "exterior",
    "balcony_terrace": "exterior",
    "garden": "exterior",
    "staircase_interior": "common",
    "staircase_exterior": "common",
    "floor_plan": "plan",
    "site_plan": "plan",
    "property_document": "plan",
    "other": "other",
}

# The full tag space (taxonomy order), derived from the grouping so the two can't drift.
ROOM_TYPES: tuple[str, ...] = tuple(ROOM_FAMILIES)

SITE_PLAN_ROOM_TYPE = "site_plan"
FLOOR_PLAN_ROOM_TYPE = "floor_plan"

# Cross-category merge compatibility. A sale ≠ a rental and (by default) a flat ≠ a house,
# so the merge's `CategoryClash` gate hard-rejects a category_main mismatch. The sanctioned
# cross-types, irrespective of sub-type, are the three pairs of dum, komercni and pozemek: a
# building listed as a house on one portal and commercial on another is one real-world
# property, and so is a plot with a house on it listed as a house on one portal and as land
# on another (operator, 2026-10-04: "Pozemky can merge with houses or komerční"). A byt and an
# ostatni merge with no other category. Lives here (pure, no heavy imports) so
# property_identity can share it without an import cycle.
_CROSS_TYPE_OK: frozenset[frozenset[str]] = frozenset({
    frozenset({"dum", "komercni"}),
    frozenset({"pozemek", "dum"}),
    frozenset({"pozemek", "komercni"}),
})


def category_main_compatible(a_cat: str | None, b_cat: str | None) -> bool:
    """True if two category_main values may be one property: equal, either NULL (unknown), or
    any two of dum, komercni and pozemek."""
    if a_cat is None or b_cat is None or a_cat == b_cat:
        return True
    return frozenset({a_cat, b_cat}) in _CROSS_TYPE_OK


def family_of(tag: str | None) -> str | None:
    """The family a logical_tag belongs to, or None for an unknown / NULL tag."""
    return ROOM_FAMILIES.get(tag) if tag else None
