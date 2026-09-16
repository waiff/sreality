"""GRADE — step 3 of 4: one granularity, one confidence, one radius.

The whole step is two tables and twenty lines of rule, which is the point of W2-a: before
it, this was `precision.py` (four orthogonal axes, a declared-cap ladder, a collision cap
read from a stamped epoch, a `position_quality_class`) plus `uncertainty.py` (184 lines
resolving `location_uncertainty_policy` through four derivations). Three of the four axes
are not on the answer table any more and the policy table is deleted, so the radius is a
per-level constant in code — the values the v1 seed shipped, which were engineering
judgement from the start and were never calibrated.

**Confidence answers one question: how many INDEPENDENT fields agreed with the entity BIND
landed on.** Not "how good is the coordinate" — that is the radius. A declared-`gps` pin
with no street text still grades by its obec-level identity, because a street filter and a
dedup key ask about the ADDRESS.
"""

from __future__ import annotations

from location_data.resolver.bind import (
    LOW_CONFIDENCE_QUALIFIERS,
    REGISTRY_PIN_CONFLICT_M,
    DeclaredPrecision,
)
from location_data.resolver.types import Binding, Grade, Position, cap_confidence

# The v1 `location_uncertainty_policy` seeds (migrations 383 + 491), collapsed to one number
# per LEVEL. UNCALIBRATED by design: these are geometric bounds, never `r95_empirical` —
# calling an uncalibrated number a 95 % containment radius would be a false probability
# statement. The four admin levels used to derive from the polygon's own
# `containment_radius_m`; that read went with the geometry loader's boundary questions, so
# they take the order-of-magnitude bound for a unit of that size.
RADIUS_M: dict[str, float] = {
    "address_point": 10.0,          # 383: registry_point / address_point
    "building": 15.0,               # 383: registry_point / building
    "parcel": 25.0,                 # 383: registry_point / parcel
    "street_segment": 100.0,        # 383: portal_pin / street_segment
    "street": 300.0,                # 383 + 491: street centroid
    "cast_obce_or_quarter": 750.0,  # 383: blur fallback, quarter-level
    "obec": 1_000.0,                # 383: blur fallback, obec-level
    "okres": 25_000.0,
    "kraj": 60_000.0,
    "country": 250_000.0,
    "unknown": 250_000.0,           # 383: the CZ-scale sentinel for a row with no position
}

# A portal's precision signal reaches the CONFIDENCE and nothing else (W18). It used to
# reach the granularity too, through a `DECLARED_CAP` ladder, and that ladder is deleted
# rather than merely bypassed: once the cap was restricted to a grain the PIN established —
# BIND's R7/R8 rungs, whose grain is `obec`, plus the unbound `unknown` — every value in it
# was at or coarser than the grain it could reach, so it changed no answer on any input.
# Brute-forced over both reachable grains × every label × the blurred fallback: zero grains
# move. A table that cannot change an output is a rail that reads as enforced and is not,
# which is exactly the defect class this subsystem keeps paying for. What a declaration still
# does is `confidence`, below: a blurred pin is a weak witness however good the bind is.

def grade(
    binding: Binding,
    position: Position,
    *,
    declared: DeclaredPrecision,
) -> Grade:
    """**THE GRANULARITY IS THE BIND'S, FULL STOP** (W18). A portal's declared precision is a
    statement about its own COORDINATE: it does not say the ad named no street, and it cannot
    un-say what RÚIAN holds about the street the ad named — so it reaches the CONFIDENCE (a
    blurred pin is a weak witness however good the bind is) and never the grain.

    Two cuts of this rule were wrong before it came out. Keying it on the elected POSITION
    inverted the two labels that are capped but NOT blurred — idnes' `no_exact_address`
    (66,165 listings) and sreality's `not_address` (13,176): a pin that AGREED with the bound
    street stayed the position and was capped to `cast_obce_or_quarter` at 750 m, while a pin
    that CONTRADICTED it lost to the street point and graded `street` at 300 m, so the
    better-evidenced row graded coarser. Keying it on the PIN-DERIVED rungs instead was
    correct and inert — their grain is already `obec` — so the table went with the rule.
    """
    granularity = binding.granularity if binding.bound else "unknown"
    return Grade(
        granularity=granularity,
        match_confidence=confidence(binding, position, blurred=declared.blurred),
        uncertainty_radius_m=radius_m(granularity, floor_m=position.extent_m),
    )


def confidence(binding: Binding, position: Position, *, blurred: bool) -> str:
    """The table. `agreed` is BIND's list of INDEPENDENT fields that matched the entity."""
    if not binding.bound:
        return "low"
    if binding.ambiguous or set(binding.relaxations) & LOW_CONFIDENCE_QUALIFIERS:
        return "low"  # a tie-break or a post-town guess decided it, not a field
    agreed = len(binding.agreed)
    if binding.target_kind == "address_point" and _pin_corroborates(position):
        value = "exact"
    elif agreed >= 2:
        value = "high"
    elif agreed == 1:
        value = "medium"
    else:
        value = "low"
    if blurred or _pin_conflicts(position) or position.pin_overridden:
        # W18: an exact pin overridden by the street is the row disagreeing with itself —
        # CHECK says so in `disputed`, and a disagreement may not be served above `medium`.
        value = cap_confidence(value, "medium")
    return value


def radius_m(granularity: str, *, floor_m: float | None = None) -> float:
    """The level's constant, never smaller than what actually placed the row (W18).

    A street is not a point: when the row sits at the centroid of a street's address points,
    the honest radius is the one that reaches its FARTHEST door — 1,288 m for Jiráskova in
    Mladá Boleslav, where the level constant says 300. The floor only ever RAISES the number,
    so every other row keeps exactly the radius it had.
    """
    return max(RADIUS_M[granularity], floor_m or 0.0)


def _pin_corroborates(position: Position) -> bool:
    """An address point whose portal pin (if any) lands within the conflict threshold."""
    return position.origin == "registry_point" and not _pin_conflicts(position)


def _pin_conflicts(position: Position) -> bool:
    distance = position.registry_pin_distance_m
    return distance is not None and distance > REGISTRY_PIN_CONFLICT_M
