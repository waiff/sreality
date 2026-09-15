"""Single source of truth for the headline `area_m2` and its `area_basis` stamp.

`area_m2` is POLYMORPHIC by design: the interior area for a dwelling
(byt / dum / komercni), the PARCEL for land. Each parser maps its own portal's
labels onto the typed measures (usable / floor / total / plot) and passes whatever
free-text guess it has left as `fallback`; the precedence that picks the
headline lives HERE so it can never diverge per portal (rule 21):

    dwelling:  usable -> floor -> total -> fallback ('unknown')
    pozemek:   plot   -> total -> usable -> floor -> fallback (always 'plot')

`plot` is the portal's own parcel measure ("plocha pozemku" / `surfaceLand` /
`estate_area`). EVERY parser hands it over, including the six whose land pages
happen to state the parcel under a label that already reached `total` — the
precedence is one rule fed the same way everywhere (rule 21), never a per-portal
arrangement of which argument a parcel arrives in. It leads on land because it is
the labelled parcel, while a land page's "celková plocha" is the same number
under a weaker label. It is NEVER a dwelling's headline: a house's plot sits beside
its floor area in `estate_area` and the dwelling arm does not read it at all.

`area_basis` is a PROVENANCE STAMP — an observation of which physical area the
column already holds. It never changes the value, which is why it stays out of
every content hash: stamping it must not churn a single snapshot (rule 2).

A 0 m2 measure is a form placeholder, never a measurement, so it is skipped the
same way the per-portal `or` chains this replaces skipped it. The same arm
carries the measure's lower validity bound: on byt / dum / komercni a headline
under MIN_AREA_M2 is a parse artifact (a title-number garble, a per-m2 note read
as the area), never a unit, so the resolver declines it and tries the next
measure. MAX_AREA_M2 is the same idea from above, for EVERY category: `area_m2`
is `numeric(7,1)`, so a measure at or beyond 10^6 cannot be stored at all — the
write boundary NULLs it (`scraper.db.sane_listing_numerics`). Declining it HERE
instead means the resolver falls through to the next measure and, crucially,
never stamps a basis for a value the row will not hold; a parcel of 16,809,800 m2
is real on the portal and belongs in `estate_area` (numeric(9,1)), not in the
headline.

BOTH bounds live HERE, not at the write boundary, because a refused measure has
to reach the content hash — a value dropped after hashing would leave `listings`
disagreeing with its own newest snapshot forever (rule 2/8).
"""

from __future__ import annotations

AREA_BASES: frozenset[str] = frozenset({"usable", "floor", "total", "plot", "unknown"})

LAND_CATEGORIES: frozenset[str] = frozenset({"pozemek"})

# Where a sub-MIN_AREA_M2 headline cannot be a real measurement. `ostatni` (a
# 3 m2 cellar, a 2 m2 parking bay) and an unclassified row are deliberately NOT
# bounded, and neither is land: 202 active parcels really are under 5 m2.
BOUNDED_CATEGORIES: frozenset[str] = frozenset({"byt", "dum", "komercni"})
MIN_AREA_M2 = 5.0

# The first value `listings.area_m2` (numeric(7,1)) cannot store. Kept equal to
# `scraper.db._NUMERIC_ABS_MAX["area_m2"]` — which is what NULLs an out-of-range
# value at the write boundary — by `tests/scraper/test_area.py`, so the two can
# never drift; spelled here rather than imported because this module is
# stdlib-only and must not pull in psycopg.
MAX_AREA_M2 = 1_000_000.0


def derive_headline_area(
    *,
    category_main: str | None,
    usable: float | None = None,
    floor: float | None = None,
    total: float | None = None,
    plot: float | None = None,
    fallback: float | None = None,
) -> tuple[float | None, str | None]:
    """Return (area_m2, area_basis) for one listing. See module docstring."""
    if category_main in LAND_CATEGORIES:
        # A parcel has no interior: whichever measure the page carried IS the plot.
        # `plot` leads because it is the parcel under its own label; `total` follows
        # because a land page's "celková plocha" is that same parcel, and a stray
        # "užitná plocha" on one is a mislabel of it again.
        for value in (plot, total, usable, floor, fallback):
            if value and value < MAX_AREA_M2:
                return value, "plot"
        return None, None
    min_m2 = MIN_AREA_M2 if category_main in BOUNDED_CATEGORIES else 0.0
    for value, basis in (
        (usable, "usable"),
        (floor, "floor"),
        (total, "total"),
        (fallback, "unknown"),
    ):
        if value and min_m2 <= value < MAX_AREA_M2:
            return value, basis
    return None, None
