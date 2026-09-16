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
from location_data.resolver.types import Binding, Grade, GranularityRank, Position, cap_confidence

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

# Every portal precision signal is an UPPER BOUND, never a certification: mmreality's one
# `accurate: true` row in the corpus is wrong and its one correct row is `accurate: false`.
# A label the contract does not map is NOT a cap — inventing one is as wrong as ignoring one.
DECLARED_CAP: dict[str, str] = {
    "gps": "address_point", "address": "address_point", "exact": "address_point",
    "presna": "address_point", "rooftop": "building",
    "street": "street", "approximate": "street", "priblizna": "street", "estimated": "street",
    "ward": "cast_obce_or_quarter", "quarter": "cast_obce_or_quarter",
    "citypart": "cast_obce_or_quarter", "area": "cast_obce_or_quarter",
    "polygon": "cast_obce_or_quarter",
    # idnes' "Na mapě nezobrazujeme přesnou adresu" disclaimer.
    "no_exact_address": "cast_obce_or_quarter",
    # sreality's LEGACY `locality.accuracy`, a two-value field: `address` is already capped
    # above, and `not_address` rides with `map.type: geometry` on every row sampled — the
    # pin is the centroid of a drawn quarter polygon, not a blurred address.
    "not_address": "cast_obce_or_quarter",
    "regional": "obec", "municipality": "obec", "obec": "obec",
    # --- W1-c: the rest of what the nine slim contracts emit.
    # bazos' maps anchor says "Přibližná lokalita" on every ad; the contract caps that pin at
    # `granularity_max: obec` (bazos.yaml, bzs.det.blur_hint), which is the rung here.
    "approximate_location": "obec",
    # maxima draws its own imprecision as a SHAPE (maxima.yaml, mx.det.map_geometry): a line
    # is a street's worth of it, a circle a quarter's. `point` is deliberately absent — an
    # unmapped label certifies nothing, and a drawn point is the one shape whose grade the
    # portal does not state (it is a marker, not a measurement). `accurate` is absent for the
    # other reason: it is PRECISE, and a cap row would certify a rung the flag does not.
    "linestring": "street",
    "circle": "cast_obce_or_quarter",
}


def grade(
    binding: Binding,
    position: Position,
    *,
    declared: DeclaredPrecision,
    rank: GranularityRank,
) -> Grade:
    """**THE DECLARED CAP IS A STATEMENT ABOUT THE PIN, so it caps only a grain the PIN
    ESTABLISHED** — BIND's two pin-derived rungs, R7 and R8 (W18).

    A portal declaring "Přibližná lokalita" is telling you its COORDINATE is fuzzy. It is not
    telling you the ad named no street, and it cannot un-tell you what RÚIAN says about the
    street the ad named. So a grain that came out of a REGISTER BIND — an address point, a
    street, a named unit — is never coarsened by it, whichever point was elected.

    Keying this on the POSITION instead (the first cut of W18) inverted two labels that are
    capped but NOT blurred — idnes' `no_exact_address` (66,165 listings) and sreality's
    `not_address` (13,176). A pin that AGREED with the bound street stayed the position and
    was capped to `cast_obce_or_quarter` at 750 m, while a pin that CONTRADICTED it lost to
    the street point and graded `street` at 300 m: the better-evidenced row graded coarser.

    What the declaration still does is lower CONFIDENCE — a blurred pin is a weak witness
    however good the bind is, and `confidence` caps such a row at `medium`.
    """
    granularity = binding.granularity if binding.bound else "unknown"
    if binding.pin_derived or not binding.bound:
        capped = DECLARED_CAP.get(declared.label or "")
        if capped is not None:
            granularity = rank.coarser_of(granularity, capped)
        elif declared.blurred:
            # A blurred declaration whose label this ladder does not know still says "not
            # address-grade": it takes the same generic fallback a bare blur_hint takes.
            granularity = rank.coarser_of(granularity, "street")
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
