"""Single source of truth for the image room/plot tag taxonomy and its FAMILY
grouping.

Pure data — no heavy imports — so both the classifier (`toolkit.image_classification`,
which emits these tags) and the dedup matcher (`toolkit.dedup_engine`, which decides how
each family participates in a merge) read ONE definition. The CLIP tagger's
anchor→tag collapse lives in `data/clip_taxonomy.json`; this module groups the resulting
`logical_tag` values into the families the dedup engine reasons about.
"""

from __future__ import annotations

# Every logical_tag the CLIP tagger / LLM classifier can emit, grouped into a FAMILY:
#   interior — a unit's own rooms; the strongest same-flat signal (used ALONE for byt).
#   exterior — facade / outdoor shots a whole development reuses across its units.
#   common   — SHARED building circulation (stairwells): every unit in a building shows the
#              same one, so it's never a unit identifier — excluded like exterior/plan.
#   plan     — floor / site plans; shared templates, never a perceptual-match signal.
#   other    — unclassifiable content; treated as unknown (counts, never excluded).
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

# Comparison priority for the forensic / pHash image layers: most DISTINCTIVE rooms
# first (kitchen + bathroom are unit-specific; bedroom / hallway are generic and
# interchangeable). For byt ONLY these interior rooms are compared at all.
INTERIOR_PRIORITY: tuple[str, ...] = (
    "kitchen", "bathroom", "toilet", "living_room", "bedroom", "hallway",
)
INTERIOR_ROOM_TYPES: frozenset[str] = frozenset(INTERIOR_PRIORITY)

# Comparison priority for categories that DO use exterior images (house / land /
# commercial): the interior rooms first, then outdoor/facade, generic bedroom last.
FULL_PRIORITY: tuple[str, ...] = (
    "kitchen", "bathroom", "toilet", "living_room", "hallway",
    "balcony_terrace", "garden", "exterior_facade", "bedroom",
)

# House / commercial: the FACADE is the building's identity, so it leads the perceptual +
# forensic order; then the interior rooms, then the rest.
HOUSE_PRIORITY: tuple[str, ...] = (
    "exterior_facade", "kitchen", "bathroom", "living_room", "toilet",
    "garden", "balcony_terrace", "hallway", "bedroom",
)

# Land / plot: the SITE PLAN is the plot's identity (the site-plan development guard reads
# it), then outdoor views. Plots rarely have interior rooms.
LAND_PRIORITY: tuple[str, ...] = (
    "site_plan", "exterior_facade", "garden", "floor_plan",
)

# Tags excluded from a byt perceptual / cosine MERGE signal: the exterior + common + plan
# families (a development/building reuses these across distinct units). 'other' / untagged are
# deliberately NOT excluded — only KNOWN-shared images are dropped.
NON_INTERIOR_TAGS: tuple[str, ...] = tuple(
    t for t, fam in ROOM_FAMILIES.items() if fam in ("exterior", "common", "plan")
)

# The most distinctive rooms: a SINGLE near-identical pHash match on one of these is
# enough to merge (operator policy), versus the >=2 generic-image matches otherwise.
DISTINCTIVE_ROOMS: frozenset[str] = frozenset({"kitchen", "bathroom"})

SITE_PLAN_ROOM_TYPE = "site_plan"
FLOOR_PLAN_ROOM_TYPE = "floor_plan"

# Cross-category merge compatibility. A sale ≠ a rental and (by default) a flat ≠ a house,
# so the dedup classifiers AND the merge_properties chokepoint hard-reject a category_main
# mismatch. The ONE sanctioned cross-type is dum <-> komercni (a building listed as a house
# on one portal and commercial on another is the same real-world property) — irrespective
# of sub-type. Lives here (pure, no heavy imports) so dedup_engine AND property_identity can
# share it without an import cycle.
_CROSS_TYPE_OK: frozenset[frozenset[str]] = frozenset({frozenset({"dum", "komercni"})})


def category_main_compatible(a_cat: str | None, b_cat: str | None) -> bool:
    """True if two category_main values may be the same property. Equal (or either NULL =
    unknown) is compatible; the only allowed cross-type is dum <-> komercni."""
    if a_cat is None or b_cat is None or a_cat == b_cat:
        return True
    return frozenset({a_cat, b_cat}) in _CROSS_TYPE_OK


def family_of(tag: str | None) -> str | None:
    """The family a logical_tag belongs to, or None for an unknown / NULL tag."""
    return ROOM_FAMILIES.get(tag) if tag else None
