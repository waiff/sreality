"""THE per-m² measure, in Python. One measure, one definition, one label.

`public.measure_price_per_m2(price, area, category_main, category_type)` and
`public.measure_price_per_m2_basis(category_main, category_type)` (migration
425) are the single named per-m² measure and its single named label. This
module is their Python face: it renders the SQL that calls them, mirrors their
resolution order for rows that never touched Postgres, and owns the vocabulary,
the floors and the unit strings. Nothing outside it may spell `price / area`,
and no surface may render a per-m² number without the basis that names its unit.

  Numerator    the listing's (or property's) price in CZK.
  Denominator  `area_m2`, POLYMORPHIC by design — floor area for byt / dum /
               komercni, PLOT area for pozemek. `listings.area_basis`
               (migration 423) records which; the *measure's* basis is resolved
               from (category_main, category_type) and NEVER from `price_unit`,
               which is a four-spelling duplicate of category_type, not a
               per-area unit.
  Unit         CZK per m² — per MONTH on the rent basis, capital otherwise.
  Validity     NULL price, NULL or non-positive area, an undecidable basis, or
               a price below its basis floor all yield NULL. A visible gap,
               never a guess. `price_czk` itself stays faithful to the source:
               the floors live in the measure, not at the write boundary.

WHY `_scale` CANNOT BE CALLED UNIT-BLIND
`api/estimate_yield.py` turns a per-m² percentile into a headline number by
multiplying it by the subject's area. That product is a monthly rent or a
purchase price depending ONLY on the percentile's basis — the arithmetic is
identical either way, so a mixed sale+rent cohort produces a confident number
that means nothing. `require_scalable_basis` is the check that makes writing
that call impossible: it takes the cohort's basis, it takes what the caller
intends to call the product, and it raises rather than let the two disagree.
Rule 22 makes a mixed cohort one click away, so this is not a theoretical arm.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

# --- the vocabulary -------------------------------------------------------
# Exactly the four values measure_price_per_m2_basis can return (the fourth
# being NULL). Spelled identically to the SQL so `grep` finds both.

SALE_CAPITAL_CZK_M2 = "sale_capital_czk_m2"
RENT_MONTHLY_CZK_M2 = "rent_monthly_czk_m2"
LAND_CAPITAL_CZK_M2 = "land_capital_czk_m2"

Ppm2Basis = Literal[
    "sale_capital_czk_m2",
    "rent_monthly_czk_m2",
    "land_capital_czk_m2",
]

PPM2_BASES: tuple[str, ...] = (
    SALE_CAPITAL_CZK_M2,
    RENT_MONTHLY_CZK_M2,
    LAND_CAPITAL_CZK_M2,
)

# Two non-basis states a COHORT can be in. Neither is a basis: they are the two
# ways a set of rows can fail to have one, and both must be rendered as a gap.
#   mixed   — the rows disagree; one blanket unit would be a lie (rule 22: the
#             default Browse cohort is sale+rent).
#   unknown — nothing said. Client-supplied rows (POST /tools/analyze_distribution
#             takes caller-provided listings) carry no basis at all; the honest
#             answer is "unknown", never a default of sale.
BASIS_MIXED = "mixed"
BASIS_UNKNOWN = "unknown"

# The key a basis-bearing row dict carries it under, everywhere.
PPM2_BASIS_KEY = "price_per_m2_basis"

# The category vocabulary the basis resolves from. category_type has FOUR live
# values; prodej / drazba / podil are all CAPITAL and share one basis. The
# capital list is an enumerated allowlist, so an unknown future value yields a
# visible NULL rather than a silent guess.
RENT_CATEGORY_TYPE = "pronajem"
CAPITAL_CATEGORY_TYPES: tuple[str, ...] = ("prodej", "drazba", "podil")
LAND_CATEGORY_MAIN = "pozemek"

# Per-basis floors, on the PRICE (the numerator), matching migration 425's
# CASE arms exactly. Land is deliberately unfloored: a cheap plot is a real
# plot, while a 136 Kč "commercial rental" is a portal artefact.
PPM2_PRICE_FLOOR_CZK: dict[str, float | None] = {
    SALE_CAPITAL_CZK_M2: 100_000.0,
    RENT_MONTHLY_CZK_M2: 1_000.0,
    LAND_CAPITAL_CZK_M2: None,
}

# The unit string for each basis. Czech, because every surface that renders one
# is Czech-facing. `mixed` and `unknown` deliberately have NO unit — that is the
# whole point of naming them.
PPM2_UNIT_CS: dict[str, str] = {
    SALE_CAPITAL_CZK_M2: "Kč/m²",
    RENT_MONTHLY_CZK_M2: "Kč/m²/měs",
    LAND_CAPITAL_CZK_M2: "Kč/m² pozemku",
}

# What multiplying a per-m² percentile by an area is allowed to be CALLED.
# A land per-m² scales to a capital price like any other sale basis.
_SCALABLE_BASES_BY_KIND: dict[str, tuple[str, ...]] = {
    "rent": (RENT_MONTHLY_CZK_M2,),
    "sale": (SALE_CAPITAL_CZK_M2, LAND_CAPITAL_CZK_M2),
}


class MeasureBasisError(ValueError):
    """A per-m² number was about to be used without a single, agreeing basis."""


# --- SQL fragments --------------------------------------------------------
# `alias` is required: there is no zero-arg variant to fall back to, so a
# unit-blind call cannot be written (the W8 rail's first part).


def per_m2_sql(alias: str) -> str:
    """The measure over a `listings`-shaped alias.

    ALWAYS the four-argument call, never `{alias}.price_per_m2`: every caller
    of this helper reads from `listings`, which has no such column, so the
    column spelling fails at PREPARE rather than at review.
    """
    return (
        f"measure_price_per_m2({alias}.price_czk::numeric, "
        f"{alias}.area_m2::numeric, {alias}.category_main, {alias}.category_type)"
    )


def per_m2_basis_sql(alias: str) -> str:
    """The measure's label over a `listings`-shaped alias."""
    return (
        f"measure_price_per_m2_basis({alias}.category_main, {alias}.category_type)"
    )


# --- the Python mirror ----------------------------------------------------


def ppm2_basis(
    category_main: str | None, category_type: str | None
) -> str | None:
    """Mirror of `measure_price_per_m2_basis`, same rent-first resolution order.

    For rows that never came from Postgres (a filter spec, an agent payload).
    Returns None when the basis is undecidable — including for a NULL
    category_type, which rule 22 makes a normal, meaningful state.
    """
    if category_type == RENT_CATEGORY_TYPE:
        return RENT_MONTHLY_CZK_M2
    if category_type in CAPITAL_CATEGORY_TYPES:
        if category_main == LAND_CATEGORY_MAIN:
            return LAND_CAPITAL_CZK_M2
        return SALE_CAPITAL_CZK_M2
    return None


def price_floor_czk(basis: str | None) -> float | None:
    """The price below which `basis` yields no measure. None = unfloored."""
    return PPM2_PRICE_FLOOR_CZK.get(basis or "", None)


def unit_label(basis: str | None) -> str | None:
    """The Czech unit for `basis`; None for mixed, unknown and undecidable."""
    return PPM2_UNIT_CS.get(basis or "")


# --- cohort-level resolution ---------------------------------------------


def cohort_basis(rows: Iterable[Mapping[str, Any]]) -> str:
    """The single basis shared by `rows`, else BASIS_MIXED / BASIS_UNKNOWN.

    Pass only the rows that actually BACK the number being labelled — a row
    dropped for a NULL value contributes no unit. One unlabelled row is enough
    to make the whole cohort unknown: it could be anything, and a cohort that is
    90% sale is still not a sale cohort.
    """
    seen: set[str] = set()
    saw_row = False
    for row in rows:
        saw_row = True
        value = row.get(PPM2_BASIS_KEY)
        if not value:
            return BASIS_UNKNOWN
        seen.add(str(value))
    if not saw_row or not seen:
        return BASIS_UNKNOWN
    if len(seen) == 1:
        return next(iter(seen))
    return BASIS_MIXED


def require_scalable_basis(basis: str | None, *, estimate_kind: str) -> str:
    """Return `basis`, or raise if it cannot be scaled into `estimate_kind`.

    Raises on mixed, unknown, unrecognised, and on a basis that contradicts what
    the product is about to be called — a monthly Kč/m² multiplied by an area is
    a monthly rent and can never be reported as a sale price.
    """
    allowed = _SCALABLE_BASES_BY_KIND.get(estimate_kind)
    if allowed is None:
        raise MeasureBasisError(
            f"unknown estimate_kind {estimate_kind!r}: cannot decide which "
            f"per-m² basis may be scaled into it"
        )
    if basis in allowed:
        return str(basis)
    if basis in (BASIS_MIXED, BASIS_UNKNOWN, None, ""):
        raise MeasureBasisError(
            f"cohort has no single per-m² basis (basis={basis!r}); refusing to "
            f"scale it into a {estimate_kind} figure. Pin category_main + "
            f"category_type so the cohort resolves to one basis."
        )
    raise MeasureBasisError(
        f"per-m² basis {basis!r} cannot be scaled into a {estimate_kind} "
        f"figure (allowed: {', '.join(allowed)})"
    )
