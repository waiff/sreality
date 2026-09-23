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

`parse_area_text` is the other half of the same idea, one level down: the ONE
grammar that turns a portal's area text into a number. It lives beside the
precedence because both are the same rule — what an area IS — and five parsers
each owning a private copy is exactly what rule 21 forbids. It was five copies
until W19, four of them the naive form that matched the first bare digit run before
an `m²` and so
read "5 870 m²" as 870. The fixed grammar is idnes's — proven in production since its
own truncation incident — WIDENED here to the separators the other portals emit: the
narrow no-break space and the thin space idnes never had to handle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The separators a Czech portal renders between a number's digit groups: ordinary
# space, NBSP, narrow NBSP, thin space, and the zero-width joiners idnes emits
# inside a rendered figure. One class, because there is one grammar (rule 21).
AREA_THOUSANDS_SEPS = "\u0020\u00a0\u202f\u2009\u200b\u200c\u200d\u2060"

# An area token immediately before "m2" / "m²" / "m 2". The FIRST alternative is the
# whole point: it accepts the Czech spaced-thousands form ("5 870 m²") that every
# portal renders, and without it `search` starts INSIDE the number and truncates
# 5870 -> 870 (idnes 8k+ rows in 2026-08; ceskereality 17,207 + realitymix 13,164 in
# W19's measurement). The lookbehind keeps the grouped form from swallowing the digit
# in front of it — a disposition ("3+1 174 m²" stays 174, never 1174), another
# number, or a decimal tail — while still allowing a match to START at a real
# number's first digit.
# The NUMBER half, shared: the lane's quote check builds on the same shape so the two
# never disagree on what a figure is (rule 21, one grammar). Three forms, in order:
# spaced thousands ("5 870 m²", with an optional decimal tail), DOTTED thousands — a
# dot followed by exactly three digits, the way brokers key "1.910 m2" / "12.100 m2" /
# "1.994,71 m²" (Czech decimals take a comma, so a 3-digit group after a dot is never a
# fraction; "1.5 m2" keeps its dot as the decimal it is) — and a plain number.
AREA_NUMBER_SRC = (
    rf"\d{{1,3}}(?:[{AREA_THOUSANDS_SEPS}]\d{{3}})+(?:[.,]\d+)?"
    rf"|\d{{1,3}}(?:\.\d{{3}})+(?:,\d+)?"
    rf"|\d+(?:[.,]\d+)?"
)
_DOTTED_THOUSANDS_RE = re.compile(r"^\d{1,3}(?:\.\d{3})+(?:,\d+)?$")
AREA_TEXT_RE = re.compile(
    rf"(?<![\d+.,])"
    rf"({AREA_NUMBER_SRC})"
    rf"\s*m(?:2|²|\s*2)\b",
    re.IGNORECASE,
)


def area_token_to_float(token: str) -> float:
    """One matched number token -> its value: separators stripped, a dotted-thousands
    token's dots with them, the decimal comma read as a point."""
    for sep in AREA_THOUSANDS_SEPS:
        token = token.replace(sep, "")
    if _DOTTED_THOUSANDS_RE.match(token):
        token = token.replace(".", "")
    return float(token.replace(",", "."))


def parse_area_text(text: str | None) -> float | None:
    """The first area figure in `text`, in m² — or None when it carries none.

    Requires the unit, so a per-m² price ("Cena za m2: 7 759 CZK") reads as no area
    at all rather than as its own number: the digits sit AFTER the unit there.
    """
    if not text:
        return None
    match = AREA_TEXT_RE.search(text)
    if not match:
        return None
    return area_token_to_float(match.group(1))


@dataclass(frozen=True, slots=True)
class PortalAreas:
    """Every area column one detail page yields, plus the headline the rule picked.

    ONE shape for every portal, so a portal's key precedence — WHICH of its spec cells is
    the usable measure, which is the parcel, in what order — can be spelled once and read
    twice: by `parse_detail` on a live fetch, and by the W19 heal re-deriving the same
    columns from `listings.raw_json`, which holds that parser's own latest reading of the
    live page. Two copies of a key order is the same defect as two copies of the number
    grammar, one level up (rule 21).

    CONSTRUCTION IS ALSO THE VALIDITY BOUND FOR THE THREE SIDE COLUMNS, and it has to be,
    because it is the last point BEFORE the content hash. `derive_headline_area` already
    declines an out-of-range `area_m2` for exactly that reason (see the module docstring);
    `usable_area` / `estate_area` / `garden_area` had no such gate, so a value at or beyond
    `numeric(9,1)`'s ceiling — or a 0 m² form placeholder — was hashed as itself and then
    NULLed at the write boundary by `scraper.db.sane_listing_numerics`, leaving `listings`
    permanently disagreeing with its own newest snapshot (rules 2/8). Bounding here makes
    the row and its snapshot agree by construction; `sane_listing_numerics` stays the last
    guard for the parsers that do not build a `PortalAreas` (sreality, bezrealitky).
    """

    area_m2: float | None = None
    area_basis: str | None = None
    usable_area: float | None = None
    estate_area: float | None = None
    garden_area: float | None = None

    def __post_init__(self) -> None:
        for column in ("usable_area", "estate_area", "garden_area"):
            value = getattr(self, column)
            if value is not None and not 0.0 < value < MAX_SIDE_AREA_M2:
                object.__setattr__(self, column, None)


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

# The same idea for the three SIDE columns, which are `numeric(9,1)`. Kept equal
# to `scraper.db._NUMERIC_ABS_MAX["estate_area"]` (= usable_area = garden_area)
# by `tests/scraper/test_area.py`, and applied in `PortalAreas.__post_init__` —
# see the class docstring for why the bound has to sit BEFORE the hash.
MAX_SIDE_AREA_M2 = 100_000_000.0


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
