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
    "floor_plan": "plan",
    "site_plan": "plan",
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

# Tags excluded from a byt perceptual / cosine MERGE signal: the exterior + plan
# families (a development reuses these across distinct units). 'other' / untagged are
# deliberately NOT excluded — only KNOWN-shared images are dropped.
NON_INTERIOR_TAGS: tuple[str, ...] = tuple(
    t for t, fam in ROOM_FAMILIES.items() if fam in ("exterior", "plan")
)

# The most distinctive rooms: a SINGLE near-identical pHash match on one of these is
# enough to merge (operator policy), versus the >=2 generic-image matches otherwise.
DISTINCTIVE_ROOMS: frozenset[str] = frozenset({"kitchen", "bathroom"})

SITE_PLAN_ROOM_TYPE = "site_plan"
FLOOR_PLAN_ROOM_TYPE = "floor_plan"


def family_of(tag: str | None) -> str | None:
    """The family a logical_tag belongs to, or None for an unknown / NULL tag."""
    return ROOM_FAMILIES.get(tag) if tag else None
