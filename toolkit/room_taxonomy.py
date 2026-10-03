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

# Deal-type classes (operator ruling 2026-09-30). sreality alone files a share sale in its own
# "Podíly" section (category_type_cb 4 -> 'podil'); the other eight portals list the same advert
# as a plain sale, so a share sale is in the SALE class. An auction and a rental are each a class
# of their own. The engine merges a share sale with a sale only at one stated price
# (`autodedup.guards.share_price_conflict`); an operator ruling needs no price.
_DEAL_CLASS: dict[str, str] = {"podil": "prodej"}


def deal_class_of(category_type: str | None) -> str | None:
    return None if category_type is None else _DEAL_CLASS.get(category_type, category_type)


def category_type_compatible(a: str | None, b: str | None) -> bool:
    """Same deal class, or either side NULL (unknown is never a conflict)."""
    return a is None or b is None or deal_class_of(a) == deal_class_of(b)


def crosses_deal_type(a: str | None, b: str | None) -> bool:
    """Both stated, different, one class: a share sale against a sale."""
    return a is not None and b is not None and a != b and deal_class_of(a) == deal_class_of(b)


def deal_class_sql(expr: str) -> str:
    """`deal_class_of` spelled in SQL over a column expression, for the one rollup
    (`scripts.recompute_property_stats`): the class table is never copied into SQL by hand."""
    whens = " ".join(f"WHEN '{raw}' THEN '{cls}'" for raw, cls in _DEAL_CLASS.items())
    return f"CASE {expr} {whens} ELSE {expr} END"


# Cross-category merge compatibility. A flat ≠ a house (by default), so the merge's
# `CategoryClash` gate hard-rejects a category_main mismatch. The ONE
# sanctioned cross-type is dum <-> komercni (a building listed as a house on one portal and
# commercial on another is the same real-world property) — irrespective of sub-type. Lives
# here (pure, no heavy imports) so property_identity can share it without an import cycle.
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
