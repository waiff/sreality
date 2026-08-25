"""THE per-m² measure, in Python. One measure, one definition, one label.

`public.measure_price_per_m2(price, area, category_main, category_type)` and
`public.measure_price_per_m2_basis(category_main, category_type)` (migration
425) are the single named per-m² measure and its single named label. This
module is their Python face: it renders the SQL that calls them, mirrors their
resolution order for rows that never touched Postgres, and owns the vocabulary,
the floors and the unit strings. Nothing outside it may spell that division
itself, and no surface may render a per-m² number without the basis that names
its unit.

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
from dataclasses import dataclass
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

# The key a basis-bearing row dict carries it under, everywhere, and the key
# the number itself rides under beside it.
PPM2_BASIS_KEY = "price_per_m2_basis"
PPM2_VALUE_KEY = "price_per_m2"

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

    ROW-LEVEL ONLY. Both arguments are one row's concrete values, where NULL
    means "this row has no category", exactly as it does to the SQL. Never pass
    a FILTER SPEC here: there `None` means UNCONSTRAINED, and the two readings
    disagree on the capital arm — a spec that pins no `category_main` admits
    both plots and floor-area rows, which this function would collapse into a
    confident `sale_capital_czk_m2`. `spec_ppm2_basis` is that reading.
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


def spec_ppm2_basis(
    category_main: str | None, category_type: str | None
) -> str | None:
    """The basis a FILTER SPEC pins, or None when the spec leaves it open.

    Same vocabulary, different reading of None: here it means "no constraint".
    The rent arm is unchanged — rent-first resolution never consults
    `category_main`, so an unpinned category on `pronajem` is still exactly one
    basis. The capital arm is where the two readings part: without a pinned
    `category_main` the cohort admits `pozemek` (Kč/m² of PLOT) alongside
    byt / dum / komercni (Kč/m² of FLOOR), and those are two units, not one.
    Undecidable is the honest answer, and the north star's answer: a visible
    gap, never a guess.

    Prefer `cohort_basis` over the returned ROWS wherever they are in hand —
    a spec describes what was asked for, the rows describe what came back.
    """
    if category_type == RENT_CATEGORY_TYPE:
        return RENT_MONTHLY_CZK_M2
    if category_type in CAPITAL_CATEGORY_TYPES:
        if category_main is None:
            return None
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


def measure_backed(
    rows: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """The subset of `rows` that actually carries a per-m² number.

    The set `cohort_basis` must be handed: a row the measure withheld a number
    from (NULL price, NULL area, sub-floor price, undecidable basis) backs no
    figure in the envelope, so its category must not colour the envelope's unit.
    """
    return [r for r in rows if r.get(PPM2_VALUE_KEY) is not None]


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


# --- THE REGISTRY (W8: the permanent rail) --------------------------------
#
# `tests/test_measure_registry_census.py` re-counts, on every push, every
# price-over-area division and every per-m² unit literal in
# scraper / toolkit / api / scripts / frontend/src / chrome-extension/src and in
# the EFFECTIVE SQL definition of every database object. Each one must appear
# below with a stated reason, and each declared measure must carry its own
# numerator, denominator, unit and validity bounds. A 65th site reds CI.
#
# This is a census, not a ban: the measure's own SQL body divides a price by an
# area, and the unit strings have to be spelled somewhere. What the rail forbids
# is an UNDECLARED one.
#
# The `why` texts below deliberately DESCRIBE units rather than spelling them
# ("a rate per m² of PLOT", not the literal). This file is itself scanned, and a
# justification that quotes a unit would make its own edit move this file's
# count — a rail that reds on its own documentation gets switched off. If you do
# spell one here, the census will tell you exactly which number to bump.


@dataclass(frozen=True)
class Measure:
    """A named quantity, complete enough to be read without its author.

    The four fields are the north star's four halves of "one measure": what is
    on top, what is underneath, what the result is called, and when it does not
    exist. A measure missing any of them cannot be labelled, and a number that
    cannot be labelled must not be rendered.
    """

    name: str
    numerator: str
    denominator: str
    unit: str
    bounds: str


MEASURES: dict[str, Measure] = {
    "ppm2": Measure(
        name="price per m² (public.measure_price_per_m2, migration 425)",
        numerator="the listing's or property's asking price in CZK "
        "(`listings.price_czk` / `properties.current_price_czk`) — monthly on "
        "the rent basis, capital otherwise, exactly as the portal published it",
        denominator="`area_m2`, POLYMORPHIC by design: floor area for byt / "
        "dum / komercni, PLOT area for pozemek. `listings.area_basis` "
        "(migration 423) records which; the measure's own basis is resolved "
        "from (category_main, category_type) and never from `price_unit`",
        unit="CZK per m² — per MONTH on the rent basis (PPM2_UNIT_CS), capital "
        "on the sale and land bases",
        bounds="NULL price, NULL or non-positive area, an undecidable basis, "
        "or a price below its basis floor (PPM2_PRICE_FLOOR_CZK: sale "
        "100 000 CZK, rent 1 000 CZK, land unfloored) all yield NULL. A "
        "visible gap, never a guess; rounded to 2dp so every relation "
        "publishing it returns byte-identical figures",
    ),
    "fond_per_m2": Measure(
        name="fond oprav + SVJ service charge per m² (api/schemas."
        "DEFAULT_FOND_CZK_PER_M2, served per subject by api/portal_lookup)",
        numerator="the building's monthly service charge attributable to one "
        "unit, defaulted (10 CZK) rather than scraped",
        denominator="the unit's FLOOR area — never a plot: a fond cannot be "
        "levied on a parcel, which is why the server serves NULL for land "
        "rather than letting a panel multiply the rate by 905 m² of field",
        unit="CZK per m² per MONTH — the same string as the rent basis "
        "(PPM2_UNIT_CS['rent_monthly_czk_m2']), because it is the same period",
        bounds="NULL when no fond can apply to the subject at all (a plot, or "
        "an unknown area); the yield that consumes it then returns NULL too",
    ),
    "gross_yield_pct": Measure(
        name="annual gross rental yield (scraper.price_stats_metrics."
        "gross_yield_pct; the SQL twin is price_stat_growth, migration 425)",
        numerator="12 × the monthly rent rate — per-m² on both sides, so the "
        "per-m² cancels and the ratio is unit-free",
        denominator="the sale rate for the same municipality and month",
        unit="percent",
        bounds="NULL unless both rates are present and the sale rate is "
        "strictly positive; rounded to 2dp on both the Python and the SQL "
        "side so one municipality cannot have two answers",
    ),
}


# What a registered site IS, relative to its measure. Six values, no others:
KIND_DEFINES = "defines"    # this site IS the measure, or its label vocabulary
KIND_CALLS = "calls"        # consumes the measure from its one definition
KIND_LABELS = "labels"      # renders/declares the unit for a human or an agent
KIND_GUARDS = "guards"      # protects its inputs, heals them, watches, or pins
KIND_PROSE = "prose"        # an enumeration or a sentence, not arithmetic
KIND_DEBT = "debt"          # a live re-derivation not yet retired — say why

SITE_KINDS: frozenset[str] = frozenset(
    {KIND_DEFINES, KIND_CALLS, KIND_LABELS, KIND_GUARDS, KIND_PROSE, KIND_DEBT}
)


@dataclass(frozen=True)
class RegisteredSite:
    """One file (or one database object) that the census legitimately finds.

    `hits` is the exact number of matches the arm makes there. A count rather
    than a line number: line numbers rot on every edit above them, while a
    count changes only when the population does — which is precisely when a
    human should be made to look.
    """

    path: str
    arm: str          # "division" | "unit"
    hits: int
    measure: str | None
    kind: str
    why: str


REGISTERED_SITES: tuple[RegisteredSite, ...] = (
    # -- SQL: the effective definition of each database object ---------------
    RegisteredSite(
        path="migrations/425_measure_price_per_m2.sql::function:measure_price_per_m2",
        arm="division",
        hits=3,
        measure="ppm2",
        kind=KIND_DEFINES,
        why="THE measure. Three arms — rent, land, sale — each dividing the "
        "price by the area once the basis and its floor have been resolved. "
        "This is the one division in the repo that is allowed to be a "
        "definition; every other consumer calls this function.",
    ),
    RegisteredSite(
        path="migrations/083_browse_stats_price_per_m2.sql::function:browse_stats",
        arm="division",
        hits=11,
        measure="ppm2",
        kind=KIND_DEBT,
        why="KNOWN DEBT, not legitimate. An ORPHAN function: zero callers in "
        "api/, toolkit/, frontend/src/ and scripts/ (grep-verified), and no "
        "function or view in the database references it either (pg_proc + "
        "pg_get_viewdef, checked live). Superseded by browse_stats_properties "
        "(migration 378, moved onto the measure in 425). It computes eleven "
        "unfloored, basis-blind per-m² expressions. It was ALSO EXECUTE-granted "
        "to `authenticated`, i.e. REACHABLE as a PostgREST RPC by any logged-in "
        "SPA session — registering a reachable re-derivation as inert debt is "
        "the one thing this census must never do, so migration 428 revokes that "
        "grant (additive, so autonomous under the database gate). The definition "
        "now stays on disk and in the catalog but is not reachable from the "
        "perimeter. OWNER: operator. BLOCKER: approval for the `drop function` "
        "itself, which is DESTRUCTIVE and needs a pg_dump; migration 083's own "
        "text is the restore script. When it is dropped, delete this entry — "
        "the census will then require it gone.",
    ),
    RegisteredSite(
        path="migrations/354_health_image_matviews_on_listing_id.sql"
        "::materialized view:scraper_health_checks_mv",
        arm="division",
        hits=1,
        measure=None,
        kind=KIND_PROSE,
        why="Not arithmetic: the health tile's `detail` string enumerates the "
        "five fields the completeness-drift check watches — 'across price_czk "
        "/ area_m2 / geom / locality / disposition'. Slashes separating a list "
        "of column names. Unfixable in place anyway (migrations are "
        "append-only, rule #1) and not worth a DDL replay of a 300-line "
        "pg_cron matview to reword.",
    ),
    # -- Python: the measure and its vocabulary ------------------------------
    RegisteredSite(
        path="toolkit/measures.py",
        arm="unit",
        hits=6,
        measure="ppm2",
        kind=KIND_DEFINES,
        why="PPM2_UNIT_CS is THE unit table — the three Czech strings every "
        "other territory copies or reads. The remaining three are this "
        "module's own prose distinguishing a FLOOR-area rate from a PLOT-area "
        "one, "
        "which is the distinction the vocabulary exists to carry.",
    ),
    RegisteredSite(
        path="toolkit/filter_registry.py",
        arm="unit",
        hits=2,
        measure="ppm2",
        kind=KIND_LABELS,
        why="The `unit` string on min_price_per_m2 and max_price_per_m2 — the "
        "agent-facing label, read verbatim through GET /admin/filter-schema. "
        "Both rows also carry `basis=BASIS_DEPENDS_ON_CATEGORY`, because the "
        "unit alone is ~300x ambiguous between a sale and a rental cohort.",
    ),
    RegisteredSite(
        path="toolkit/comparables.py",
        arm="unit",
        hits=2,
        measure="ppm2",
        kind=KIND_CALLS,
        why="The rule-16 shared matcher emits `measure_price_per_m2(...)` via "
        "per_m2_sql(); the two literals are the docstring explaining why an "
        "unpinned category_main refuses to label a cohort — it admits plots "
        "(a rate per m² of PLOT) beside flats (a rate per m² of FLOOR).",
    ),
    RegisteredSite(
        path="toolkit/region_annotations.py",
        arm="unit",
        hits=2,
        measure="ppm2",
        kind=KIND_LABELS,
        why="The LLM prompt for the Browse > Stats box-plot annotations. The "
        "unit reaches a model, so it is a label like any other; the second "
        "literal is the refusal text for a mixed sale+rent cohort.",
    ),
    RegisteredSite(
        path="scraper/price_stats_metrics.py",
        arm="unit",
        hits=2,
        measure="gross_yield_pct",
        kind=KIND_DEFINES,
        why="THE reason the census has a second arm. `12.0 * "
        "rent_per_m2_month / sale_per_m2` names no area at all, so the "
        "division arm cannot see it; its docstring — naming the monthly rent "
        "rate and the capital sale rate — is the module's declaration of the "
        "two units it "
        "cancels, and is the canonical wording migration 425 copied into the "
        "column comments.",
    ),
    RegisteredSite(
        path="scraper/db.py",
        arm="unit",
        hits=1,
        measure="ppm2",
        kind=KIND_GUARDS,
        why="`sane_listing_numerics` clamps a 0 m² area to NULL at the write "
        "boundary — a form placeholder, never a measurement. The docstring "
        "names the consumer it protects: the measure's denominator.",
    ),
    RegisteredSite(
        path="scraper/price_text.py",
        arm="unit",
        hits=2,
        measure="ppm2",
        kind=KIND_GUARDS,
        why="`is_per_area_price()` (W1) — the one guard, shared by nine "
        "portals, that stops a per-m² NOTE in a price string being stored as the "
        "price. It has to spell the unit in order to detect it; storing a rate "
        "as a total is what made mmreality dum read ~5 700 CZK per m².",
    ),
    # -- Python: heal, watch, consume ---------------------------------------
    RegisteredSite(
        path="scripts/backfill_idnes_areas.py",
        arm="unit",
        hits=1,
        measure="ppm2",
        kind=KIND_GUARDS,
        why="A one-shot repair of the denominator (idnes areas parsed as 403 "
        "instead of 2403). The literal is the docstring stating what the "
        "defect did to every per-m² figure computed from those rows.",
    ),
    RegisteredSite(
        path="scripts/backfill_mmreality_areas.py",
        arm="unit",
        hits=1,
        measure="ppm2",
        kind=KIND_GUARDS,
        why="The W2 write pass for mmreality's totalArea-over-usableArea "
        "headline. NOT YET RUN against production (the operator runs it), so "
        "mmreality dum still reads ~5 700 CZK per m² on a 905 m² median area. The "
        "literal is the docstring quantifying that.",
    ),
    RegisteredSite(
        path="scripts/verify_pipeline.py",
        arm="unit",
        hits=4,
        measure="ppm2",
        kind=KIND_GUARDS,
        why="W9's plausibility checks (migration 427) — the median-shift, "
        "basis-floor-share and coverage detectors, plus their operator-facing "
        "labels. They watch the measure; they never recompute it.",
    ),
    RegisteredSite(
        path="api/agent.py",
        arm="unit",
        hits=5,
        measure="ppm2",
        kind=KIND_LABELS,
        why="The estimation agent's tool schema and planning prompt, which "
        "must name all three bases so the model cannot scale a capital rate "
        "into a monthly rent. A prompt is a render surface with a model as "
        "its reader.",
    ),
    RegisteredSite(
        path="api/schemas.py",
        arm="unit",
        hits=1,
        measure="ppm2",
        kind=KIND_LABELS,
        why="The docstring of the box-plot annotation request model, naming "
        "the quantity the endpoint annotates.",
    ),
    RegisteredSite(
        path="api/estimate_yield.py",
        arm="unit",
        hits=2,
        measure="ppm2",
        kind=KIND_CALLS,
        why="`_scale`'s docstring: multiplying a per-m² percentile by an area "
        "is arithmetically identical for a monthly and a capital rate, which "
        "is exactly why `require_scalable_basis` refuses to run without the "
        "basis. It consumes the measure; it does not restate it.",
    ),
    RegisteredSite(
        path="api/portal_lookup.py",
        arm="unit",
        hits=1,
        measure="fond_per_m2",
        kind=KIND_CALLS,
        why="A different measure. The docstring explains why the endpoint "
        "serves a NULL fond default for land: a fond is levied per m² of a "
        "DWELLING, and offering the rate on a 905 m² parcel would subtract "
        "~9 050 CZK/month from a yield numerator.",
    ),
    # -- Frontend + extension: the label half --------------------------------
    RegisteredSite(
        path="frontend/src/lib/measure.ts",
        arm="unit",
        hits=6,
        measure="ppm2",
        kind=KIND_DEFINES,
        why="The SPA twin of PPM2_UNIT_CS: PPM2_UNIT (suffix form) and "
        "PPM2_VALUE_LABEL (axis/legend form, naming the quantity). Every SPA "
        "surface reads these through fmtMeasuredPricePerM2; no component "
        "spells a unit of its own.",
    ),
    RegisteredSite(
        path="frontend/src/lib/filterRegistry.generated.ts",
        arm="unit",
        hits=2,
        measure="ppm2",
        kind=KIND_LABELS,
        why="Generated, never hand-edited: the mirror of the two "
        "two `unit` declarations in toolkit/filter_registry.py, kept "
        "fresh by scripts/generate_filter_registry --check in CI.",
    ),
    RegisteredSite(
        path="frontend/src/lib/pipelineChecks.ts",
        arm="unit",
        hits=3,
        measure="ppm2",
        kind=KIND_LABELS,
        why="The operator-facing display names of W9's three per-m² pipeline "
        "checks, so the health surface names the same quantity the check "
        "does.",
    ),
    RegisteredSite(
        path="frontend/src/components/region/DispositionBoxPlots.tsx",
        arm="unit",
        hits=2,
        measure="ppm2",
        kind=KIND_LABELS,
        why="The two refusal messages for a cohort with no single basis — "
        "'so the axis has a unit' and 'no recognised basis for this group'. They "
        "name the unit in order to explain its ABSENCE, "
        "which is the north star's visible gap.",
    ),
    RegisteredSite(
        path="chrome-extension/src/content.ts",
        arm="unit",
        hits=1,
        measure="ppm2",
        kind=KIND_LABELS,
        why="`CZK_PER_M2_MONTH` — a third territory that can import neither "
        "the Python nor the SPA module, so the rent-basis string is copied "
        "VERBATIM from PPM2_UNIT_CS['rent_monthly_czk_m2']. One constant "
        "labels both monthly per-m² figures the panel renders (the MF "
        "reference rent and the fond rate); the bare capital unit would "
        "misname either by a factor of twelve.",
    ),
    # -- Frontend: the tests that pin the vocabulary -------------------------
    RegisteredSite(
        path="frontend/src/lib/measure.test.ts",
        arm="unit",
        hits=5,
        measure="ppm2",
        kind=KIND_GUARDS,
        why="Pins PPM2_UNIT and PPM2_VALUE_LABEL to their exact strings, so a "
        "'harmless' relabelling of the SPA vocabulary fails a test rather "
        "than shipping.",
    ),
    RegisteredSite(
        path="frontend/src/lib/format.test.ts",
        arm="unit",
        hits=4,
        measure="ppm2",
        kind=KIND_GUARDS,
        why="Pins each basis to its exact rendered string, and — since W8 — "
        "that all THREE render pairwise differently. It asserted only "
        "rent-vs-sale before, which is how the land suffix sat as a "
        "byte-for-byte copy of the sale one: a plot rate and a floor rate are "
        "different denominators and must not share a label. Also pins the "
        "rounding and the non-breaking space.",
    ),
    RegisteredSite(
        path="frontend/src/lib/growthChoropleth.test.ts",
        arm="unit",
        hits=2,
        measure="ppm2",
        kind=KIND_GUARDS,
        why="Pins the two canonical value labels on the choropleth series — "
        "the repo's original correct basis labels, which PPM2_VALUE_LABEL was "
        "lifted from verbatim.",
    ),
    # -- SQL: the one-shot statements ----------------------------------------
    # Not object definitions and never superseded: they ran once, in order. The
    # census scans them unconditionally, because a generated column, a backfill
    # or an index expression is a second definition of the measure and a column
    # comment is a label the database catalog publishes.
    RegisteredSite(
        path="migrations/104_region_disposition_annotations.sql::statements",
        arm="unit",
        hits=8,
        measure="ppm2",
        kind=KIND_LABELS,
        why="The `insert into app_settings` that seeded the box-plot annotation "
        "system prompt. A stored prompt is a render surface with a model as its "
        "reader, and this one spells the unit eight times while telling the "
        "model NOT to assume which basis it is on. Migration 426 later replaced "
        "the row; both statements are registered, because the census reads what "
        "is in migrations/, and migrations are append-only (rule #1).",
    ),
    RegisteredSite(
        path="migrations/425_measure_price_per_m2.sql::statements",
        arm="unit",
        hits=4,
        measure="ppm2",
        kind=KIND_LABELS,
        why="`comment on column` on the stored rate columns — the observation "
        "rate, the two city-metric rates, the yield, and the three MF reference "
        "rents. The catalog is a declared label surface of this program: these "
        "are the canonical wordings, lifted from the yield docstring, and they "
        "are what an operator reading the schema sees. A future migration "
        "commenting a monthly column with the capital unit is exactly the "
        "mislabel this arm exists to red.",
    ),
    RegisteredSite(
        path="migrations/425_measure_price_per_m2.sql::statements",
        arm="division",
        hits=1,
        measure=None,
        kind=KIND_PROSE,
        why="Not arithmetic: the `do $$` block stamps a `comment on function` "
        "onto the superseded browse_stats, and that comment names the "
        "re-derivation the program removed. Prose in a catalog comment, "
        "describing the anti-pattern rather than performing it.",
    ),
    RegisteredSite(
        path="migrations/426_cohort_entry_ppm2_basis.sql::statements",
        arm="unit",
        hits=3,
        measure="ppm2",
        kind=KIND_LABELS,
        why="The `update app_settings` that replaced migration 104's prompt "
        "with the basis-aware one. The three literals are the three basis "
        "tokens mapped to the three unit strings, taught to the model as one "
        "table — the same vocabulary PPM2_UNIT_CS holds, crossing into a "
        "fourth territory that cannot import it.",
    ),
    # -- Prose the arms legitimately find ------------------------------------
    RegisteredSite(
        path="scraper/db.py",
        arm="division",
        hits=1,
        measure=None,
        kind=KIND_PROSE,
        why="Not arithmetic: `_create_singleton_property`'s docstring names the "
        "removed geo Tier-1 spatial probe by its three signals, slash-separated. "
        "A sentence about a deleted matcher, in the module that refuses to match "
        "at insert time (rule #15).",
    ),
    # -- The vocabulary arm: every file that CONSUMES the shared labels -------
    # One entry per file, hits=1 by construction. This arm exists because the
    # other two are spelling filters: a site that imports the label correctly and
    # computes the number wrongly spells nothing, so neither arm sees it. Reading
    # the vocabulary is therefore itself a census event, and the `why` has to say
    # where the NUMBER beside the label comes from.
    RegisteredSite(
        path="toolkit/measures.py",
        arm="vocab",
        hits=1,
        measure="ppm2",
        kind=KIND_DEFINES,
        why="The module that owns the vocabulary; every other consumer imports "
        "from here or copies from here.",
    ),
    RegisteredSite(
        path="frontend/src/lib/measure.ts",
        arm="vocab",
        hits=1,
        measure="ppm2",
        kind=KIND_DEFINES,
        why="The SPA twin of the vocabulary. Value-pinned against the Python "
        "table by the census, so the two cannot drift the way the land suffix "
        "drifted before W8.",
    ),
    RegisteredSite(
        path="frontend/src/lib/format.ts",
        arm="vocab",
        hits=1,
        measure="ppm2",
        kind=KIND_CALLS,
        why="`fmtMeasuredPricePerM2` — THE renderer, and the only place the SPA "
        "turns a server-computed figure plus its server-published basis into a "
        "string. Its basis argument is required, which is part (a) of the rail.",
    ),
    RegisteredSite(
        path="frontend/src/lib/growthChoropleth.ts",
        arm="vocab",
        hits=1,
        measure="ppm2",
        kind=KIND_LABELS,
        why="The choropleth legend reads the shared value labels for its two "
        "rate series; the numbers themselves are the server's growth columns, "
        "not a client-side ratio.",
    ),
    RegisteredSite(
        path="frontend/src/components/BrowseStats.tsx",
        arm="vocab",
        hits=1,
        measure="ppm2",
        kind=KIND_LABELS,
        why="The Browse stats tile labels the RPC's aggregate with the cohort "
        "basis the server resolved, and renders an empty unit — a visible gap — "
        "when the cohort has none.",
    ),
    RegisteredSite(
        path="frontend/src/components/Filters.tsx",
        arm="vocab",
        hits=1,
        measure="ppm2",
        kind=KIND_LABELS,
        why="The sidebar's per-m² bound suffix, resolved from the cohort the "
        "filter spec pins rather than from a row — the spec reading of an "
        "unpinned category, which refuses to label rather than guessing.",
    ),
    RegisteredSite(
        path="frontend/src/components/ListingMap.tsx",
        arm="vocab",
        hits=1,
        measure="ppm2",
        kind=KIND_LABELS,
        why="Map pins and the rent-map legend label the server's published "
        "figure with the server's published basis token; the map never divides "
        "anything itself.",
    ),
    RegisteredSite(
        path="frontend/src/components/estimation/RunPanel.tsx",
        arm="vocab",
        hits=1,
        measure="fond_per_m2",
        kind=KIND_LABELS,
        why="The fond oprav + SVJ input. W8 fixed it: the field carried the "
        "CAPITAL suffix on a MONTHLY charge, twelve times wrong, while the "
        "extension already rendered the same field with the monthly one. It now "
        "reads the rent-basis string from the shared map.",
    ),
    RegisteredSite(
        path="frontend/src/components/region/DispositionBoxPlots.tsx",
        arm="vocab",
        hits=1,
        measure="ppm2",
        kind=KIND_LABELS,
        why="The box-plot axis caption, and the refusal path that removes the "
        "axis entirely when the cohort spans more than one basis.",
    ),
    RegisteredSite(
        path="frontend/src/pages/Datasets.tsx",
        arm="vocab",
        hits=1,
        measure="ppm2",
        kind=KIND_LABELS,
        why="The city-metrics table headers, which must distinguish the capital "
        "column from the monthly one: both hold stored rates computed upstream "
        "by the price-stats job, not by this page.",
    ),
    RegisteredSite(
        path="frontend/src/pages/WatchdogManage.tsx",
        arm="vocab",
        hits=1,
        measure="ppm2",
        kind=KIND_LABELS,
        why="A saved watchdog filter's per-m² bound, labelled from the cohort "
        "that filter pins — and spelled out in words when the cohort spans both "
        "deal types, since rule 16 makes Watchdog and Browse share one matcher.",
    ),
    RegisteredSite(
        path="frontend/src/lib/measure.test.ts",
        arm="vocab",
        hits=1,
        measure="ppm2",
        kind=KIND_GUARDS,
        why="The vocabulary's own pinning test; it consumes every table in the "
        "module in order to assert their exact contents.",
    ),
    RegisteredSite(
        path="frontend/src/lib/growthChoropleth.test.ts",
        arm="vocab",
        hits=1,
        measure="ppm2",
        kind=KIND_GUARDS,
        why="Pins that the choropleth legend still reads the shared value "
        "labels rather than a local copy of them.",
    ),
)
