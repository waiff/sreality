"""Single source of truth for the headline `area_m2` and its `area_basis` stamp.

`area_m2` is POLYMORPHIC by design: the interior area for a dwelling
(byt / dum / komercni), the PARCEL for land. Each parser maps its own portal's
labels onto the typed measures (usable / floor / total) and passes whatever
free-text guess it has left as `fallback`; the precedence that picks the
headline lives HERE so it can never diverge per portal (rule 21):

    dwelling:  usable -> floor -> total -> fallback ('unknown')
    pozemek:   total  -> usable -> floor -> fallback (always 'plot')

`area_basis` is a PROVENANCE STAMP — an observation of which physical area the
column already holds. It never changes the value, which is why it stays out of
every content hash: stamping it must not churn a single snapshot (rule 2).

A 0 m2 measure is a form placeholder, never a measurement, so it is skipped the
same way the per-portal `or` chains this replaces skipped it. The same arm
carries the measure's lower validity bound: on byt / dum / komercni a headline
under MIN_AREA_M2 is a parse artifact (a title-number garble, a per-m2 note read
as the area), never a unit, so the resolver declines it and tries the next
measure. The bound lives HERE, not at the write boundary, because a refused
measure has to reach the content hash — a value dropped after hashing would
leave `listings` disagreeing with its own newest snapshot forever (rule 2/8).
"""

from __future__ import annotations

AREA_BASES: frozenset[str] = frozenset({"usable", "floor", "total", "plot", "unknown"})

LAND_CATEGORIES: frozenset[str] = frozenset({"pozemek"})

# Where a sub-MIN_AREA_M2 headline cannot be a real measurement. `ostatni` (a
# 3 m2 cellar, a 2 m2 parking bay) and an unclassified row are deliberately NOT
# bounded, and neither is land: 202 active parcels really are under 5 m2.
BOUNDED_CATEGORIES: frozenset[str] = frozenset({"byt", "dum", "komercni"})
MIN_AREA_M2 = 5.0


def derive_headline_area(
    *,
    category_main: str | None,
    usable: float | None = None,
    floor: float | None = None,
    total: float | None = None,
    fallback: float | None = None,
) -> tuple[float | None, str | None]:
    """Return (area_m2, area_basis) for one listing. See module docstring."""
    if category_main in LAND_CATEGORIES:
        # A parcel has no interior: whichever measure the page carried IS the plot.
        # `total` leads because a land page's "celková plocha" is the parcel, while
        # a stray "užitná plocha" on one is a mislabel of the same number.
        for value in (total, usable, floor, fallback):
            if value:
                return value, "plot"
        return None, None
    min_m2 = MIN_AREA_M2 if category_main in BOUNDED_CATEGORIES else 0.0
    for value, basis in (
        (usable, "usable"),
        (floor, "floor"),
        (total, "total"),
        (fallback, "unknown"),
    ):
        if value and value >= min_m2:
            return value, basis
    return None, None
