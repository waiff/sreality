"""GRADE — step 3 of 4: one granularity, one confidence, one radius.

**The grain is the row's FINEST KEY** (`grain_of`, W5): one writer, read off what FILL wrote;
CHECK writes none and a portal's declared precision reaches the confidence, never the grain.

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

from location_data.resolver.bind import LOW_CONFIDENCE_QUALIFIERS, REGISTRY_PIN_CONFLICT_M
from location_data.resolver.types import Binding, Fill, Position, Verdict, cap_confidence

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
    "street": 300.0,                # 383 + 491: street centroid
    "cast_obce_or_quarter": 750.0,  # 383: blur fallback, quarter-level
    "obec": 1_000.0,                # 383: blur fallback, obec-level
    "okres": 25_000.0,
    "kraj": 60_000.0,
    "country": 250_000.0,
    "unknown": 250_000.0,           # 383: the CZ-scale sentinel for a row with no position
}

# The row's keys, finest first: the first one FILL wrote names the grain.
_GRAIN_KEYS: tuple[tuple[str, str], ...] = (
    ("address_point", "ruian_adm_kod"), ("street", "ulice_kod"),
    ("cast_obce_or_quarter", "cast_obce_kod"), ("obec", "obec_kod"), ("okres", "okres_kod"),
    ("kraj", "kraj_kod"))


def grain_of(filled: Fill, verdict: Verdict) -> str:
    """The row's grain is its finest key (W5): the ONE grain writer. A foreign row is `country`
    where CHECK named the country, else `unknown`, as an undetermined one is."""
    if verdict.country_status != "cz":
        named = verdict.country_status == "foreign" and verdict.country_code
        return "country" if named else "unknown"
    return next((grain for grain, key in _GRAIN_KEYS if getattr(filled, key) is not None),
                "unknown")


def confidence(binding: Binding, position: Position, *, blurred: bool) -> str:
    """The table. `agreed` is BIND's list of INDEPENDENT fields that matched the entity."""
    if not binding.bound:
        return "low"
    if binding.ambiguous or set(binding.relaxations) & LOW_CONFIDENCE_QUALIFIERS:
        return "low"  # a tie-break or a nearest-obec sliver decided it, not a field
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
    Mladá Boleslav, where the level constant says 300. Since v5.8 FILL's placements carry the
    same floor: a část obce its farthest door (Břevnov 2.9 km against 750 m), a unit its
    polygon's reach from the point (Praha 17.6 km against 1 km). The floor only ever RAISES
    the number, and a pin-placed row has none.
    """
    return max(RADIUS_M[granularity], floor_m or 0.0)


def _pin_corroborates(position: Position) -> bool:
    """An address point whose portal pin (if any) lands within the conflict threshold."""
    return position.origin == "registry_point" and not _pin_conflicts(position)


def _pin_conflicts(position: Position) -> bool:
    distance = position.registry_pin_distance_m
    return distance is not None and distance > REGISTRY_PIN_CONFLICT_M
