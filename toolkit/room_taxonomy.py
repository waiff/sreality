"""Single source of truth for the image room/plot tag taxonomy and its FAMILY
grouping.

Pure data — no heavy imports — so both the classifier (`toolkit.image_classification`,
which emits these tags) and the dedup matcher (`toolkit.dedup_engine`, which decides how
each family participates in a merge) read ONE definition. The CLIP tagger's
anchor→tag collapse lives in `data/clip_taxonomy.json`; this module groups the resulting
`logical_tag` values into the families the dedup engine reasons about.
"""

from __future__ import annotations

from dataclasses import dataclass

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

SITE_PLAN_ROOM_TYPE = "site_plan"
FLOOR_PLAN_ROOM_TYPE = "floor_plan"


@dataclass(frozen=True)
class ImageRole:
    """A logical image tag's role in ONE dedup-engine match family (design doc §5/§6
    Session 5). Replaces three previously independent, hand-maintained mechanisms — the
    byt-only pHash exclusion + single-match override, each family's forensic-compare
    priority order, and DISTINCTIVE_DISMISS_ROOMS + the facade-flag conditional union in
    `decide_visual_dismiss` — with ONE declaration per (family, tag) that every dispatch
    function below derives from."""

    phash_vote: bool = True
    """Counted in the pHash/cosine same-photo evidence tally. False = excluded from the
    count entirely (byt's known-shared exterior/common/plan images)."""

    forensic_order: int | None = None
    """Position in this family's LLM forensic-compare priority (stop-at-first-High).
    None = this tag is never forensically compared for this family."""

    distinctive: bool = False
    """A SINGLE near-identical pHash match on this tag alone is enough to merge (vs. the
    >=2-match default) — wet rooms are unit-specific, not a shared marketing render."""

    dismiss: bool = False
    """Qualifies for `decide_visual_dismiss`'s "every qualifying room verdicted Low"
    auto-dismiss."""

    dismiss_needs_facade_flag: bool = False
    """This tag's `dismiss` role is additionally gated by the live
    `dedup_facade_dismiss_enabled` setting (today: exterior_facade, non-byt families)."""

    gate: bool = False
    """A plan-type merge VETO (the site-plan development guard / floor-plan gate) —
    structurally separate from vote/dismiss: a gate can only block or queue an
    otherwise-would-merge pair, never itself contribute evidence toward one."""


# byt: ONLY the interior rooms vote/compare at all (a development reuses facade/plan
# images across distinct units, so those never count) — the ORIGINAL INTERIOR_PRIORITY
# order, most-distinctive (wet rooms) first, generic rooms last.
_BYT_ROLES: dict[str, ImageRole] = {
    "kitchen": ImageRole(forensic_order=1, distinctive=True, dismiss=True),
    "bathroom": ImageRole(forensic_order=2, distinctive=True, dismiss=True),
    "toilet": ImageRole(forensic_order=3),
    "living_room": ImageRole(forensic_order=4),
    "bedroom": ImageRole(forensic_order=5),
    "hallway": ImageRole(forensic_order=6),
    "exterior_facade": ImageRole(phash_vote=False),
    "balcony_terrace": ImageRole(phash_vote=False),
    "garden": ImageRole(phash_vote=False),
    "staircase_interior": ImageRole(phash_vote=False),
    "staircase_exterior": ImageRole(phash_vote=False),
    "floor_plan": ImageRole(phash_vote=False, gate=True),
    "site_plan": ImageRole(phash_vote=False, gate=True),
    "property_document": ImageRole(phash_vote=False),
    "other": ImageRole(),
}

# house / commercial (dum, komercni, ostatni): the FACADE is the building's identity, so
# it leads the forensic order — the ORIGINAL HOUSE_PRIORITY. Every tag still VOTES in the
# pHash count (non-byt excludes nothing — any image can carry a house's identity); tags
# outside the forensic priority are pHash-vote-only (never forensically compared).
_HOUSE_ROLES: dict[str, ImageRole] = {
    "exterior_facade": ImageRole(forensic_order=1, dismiss_needs_facade_flag=True),
    "kitchen": ImageRole(forensic_order=2, dismiss=True),
    "bathroom": ImageRole(forensic_order=3, dismiss=True),
    "living_room": ImageRole(forensic_order=4),
    "toilet": ImageRole(forensic_order=5),
    "garden": ImageRole(forensic_order=6),
    "balcony_terrace": ImageRole(forensic_order=7),
    "hallway": ImageRole(forensic_order=8),
    "bedroom": ImageRole(forensic_order=9),
    "floor_plan": ImageRole(gate=True),
    "site_plan": ImageRole(gate=True),
    "staircase_interior": ImageRole(),
    "staircase_exterior": ImageRole(),
    "property_document": ImageRole(),
    "other": ImageRole(),
}

# pozemek (land/plot): the SITE PLAN is the plot's identity — the ORIGINAL LAND_PRIORITY.
# Plots rarely have interior rooms, so those never enter the forensic order (though they
# still pHash-vote — non-byt excludes nothing).
_POZEMEK_ROLES: dict[str, ImageRole] = {
    "site_plan": ImageRole(forensic_order=1, gate=True),
    "exterior_facade": ImageRole(forensic_order=2, dismiss_needs_facade_flag=True),
    "garden": ImageRole(forensic_order=3),
    "floor_plan": ImageRole(forensic_order=4, gate=True),
    "kitchen": ImageRole(dismiss=True),
    "bathroom": ImageRole(dismiss=True),
    "toilet": ImageRole(),
    "living_room": ImageRole(),
    "bedroom": ImageRole(),
    "hallway": ImageRole(),
    "balcony_terrace": ImageRole(),
    "staircase_interior": ImageRole(),
    "staircase_exterior": ImageRole(),
    "property_document": ImageRole(),
    "other": ImageRole(),
}

# ONE per-family registry — dum/komercni/ostatni deliberately alias the same _HOUSE_ROLES
# object (identical shape today; `default_priority_for_family`'s docstring already notes
# they're "listed separately so the operator can tune each independently" via
# `dedup_tag_priorities`, an orthogonal reordering-only mechanism — see
# `toolkit/dedup_priorities.py`).
IMAGE_ROLE_REGISTRY: dict[str, dict[str, ImageRole]] = {
    "byt": _BYT_ROLES,
    "dum": _HOUSE_ROLES,
    "komercni": _HOUSE_ROLES,
    "ostatni": _HOUSE_ROLES,
    "pozemek": _POZEMEK_ROLES,
}


def _priority_order(roles: dict[str, ImageRole]) -> tuple[str, ...]:
    ordered = [(role.forensic_order, tag) for tag, role in roles.items()
               if role.forensic_order is not None]
    ordered.sort()
    return tuple(tag for _order, tag in ordered)


def _non_voting_tags(roles: dict[str, ImageRole]) -> tuple[str, ...]:
    return tuple(tag for tag, role in roles.items() if not role.phash_vote)


def _distinctive_tags(roles: dict[str, ImageRole]) -> frozenset[str]:
    return frozenset(tag for tag, role in roles.items() if role.distinctive)


def dismiss_qualifying_tags(roles: dict[str, ImageRole], *, facade_dismiss: bool) -> frozenset[str]:
    """Tags that qualify for `decide_visual_dismiss`'s auto-dismiss check under this
    family's registry — the facade flag is a live operator setting, not static registry
    data, so it's applied here rather than baked into `ImageRole.dismiss`."""
    return frozenset(
        tag for tag, role in roles.items()
        if role.dismiss or (role.dismiss_needs_facade_flag and facade_dismiss)
    )


# The following constants are DERIVED from IMAGE_ROLE_REGISTRY (never hand-maintained
# independently) so a role change in the registry above can't drift from the tuples/sets
# every dispatch function reads — this is the whole point of Session 5's unification.

# Comparison priority for the forensic / pHash image layers: most DISTINCTIVE rooms
# first (kitchen + bathroom are unit-specific; bedroom / hallway are generic and
# interchangeable). For byt ONLY these interior rooms are compared at all.
INTERIOR_PRIORITY: tuple[str, ...] = _priority_order(_BYT_ROLES)
INTERIOR_ROOM_TYPES: frozenset[str] = frozenset(INTERIOR_PRIORITY)

# Comparison priority for categories that DO use exterior images (house / land /
# commercial): the interior rooms first, then outdoor/facade, generic bedroom last.
# NOT derived from the registry (no current dispatch function returns this tuple — see
# `toolkit/dedup_engine.py:default_priority_for_family` — it is used only by the vision
# model bake-off harness, `scripts/validate_vision_models.py`, as a broader benchmark set).
FULL_PRIORITY: tuple[str, ...] = (
    "kitchen", "bathroom", "toilet", "living_room", "hallway",
    "balcony_terrace", "garden", "exterior_facade", "bedroom",
)

# House / commercial: the FACADE is the building's identity, so it leads the perceptual +
# forensic order; then the interior rooms, then the rest.
HOUSE_PRIORITY: tuple[str, ...] = _priority_order(_HOUSE_ROLES)

# Land / plot: the SITE PLAN is the plot's identity (the site-plan development guard reads
# it), then outdoor views. Plots rarely have interior rooms.
LAND_PRIORITY: tuple[str, ...] = _priority_order(_POZEMEK_ROLES)

# Tags excluded from a byt perceptual / cosine MERGE signal: the exterior + common + plan
# families (a development/building reuses these across distinct units). 'other' / untagged are
# deliberately NOT excluded — only KNOWN-shared images are dropped.
NON_INTERIOR_TAGS: tuple[str, ...] = _non_voting_tags(_BYT_ROLES)

# The most distinctive rooms: a SINGLE near-identical pHash match on one of these is
# enough to merge (operator policy), versus the >=2 generic-image matches otherwise.
DISTINCTIVE_ROOMS: frozenset[str] = _distinctive_tags(_BYT_ROLES)

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
