"""E157/E158 (D50): identity is DEMONSTRATED, not presumed from the silence of the readers.

g8b's promotion rule is "merge unless a reader FINDS a distinguishing fact". Its safety is
therefore bounded by how much Czech prose the readers cover, and three cohorts in a row have
produced a form none of them knew. This module asks the opposite question: do the two adverts
POSITIVELY say the same thing?

  (A) `demonstration_gap` — the key facts are KNOWN on both sides and equal. An area neither
      body nor a trustworthy column states is UNKNOWN, and unknown demonstrates nothing; a
      price gap two co-live adverts never reconcile is two prices, not one asking price at two
      moments; two adverts in two towns are two objects whatever else they agree on.
  (B) `corroboration_warrant` — one piece of UNIT-grade evidence: a tight non-catalogue photo
      FILE in common, a body one advert essentially contains, or a shared rare order code.
      Agreement on public attributes is what every unit of one project has; these three are
      what only one unit has.

Both are filters on PROMOTION. The merge zone is the engine's own certified evidence and is
untouched, so the three arms are nested on merged pairs by construction (E159): L asks C only,
M asks A and C, S asks A and B and C.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, Sequence

from autodedup.body_align import rounding_equal_values
from autodedup.dataset import Listing
from autodedup.features import plot_area, rel_diff
from autodedup.guards import LAND_CATEGORY
from autodedup.settings import Settings
from autodedup.text_facts import fold, printed_areas

Feats = Mapping[str, Sequence[object]]

# The asking price ACROSS portals: two portals carrying one order differ by up to 4.3 % at p95.
PRICE_CROSS_TOL: float = 0.05
# A stored headline this many times smaller than what the body prints is not this unit's area —
# it is the terrace, the cellar or a cell the portal filled with the wrong number. bazos stores
# `area_m2 = 10.0` for a 76 m² apartment whose body says "terasou o velikosti 10 m²".
DEGENERATE_RATIO: float = 3.0

# The evidence that is about THIS UNIT rather than about the building it stands in.
UNIT_EVIDENCE: tuple[str, ...] = ("photo", "body", "code")
# And INSIDE a development the body is not one of them. `features` already says so about the
# feature — "a broker's reused boilerplate is exactly what high containment looks like between
# two units of one development" — and the Černovírské zahrady parcelling proves it: three idnes
# adverts posted two seconds apart, three distinct detail URLs, BYTE-IDENTICAL bodies, each
# 282 m² at 1,580,000, for a project the body itself calls "tři samostatné parcely", ALL THREE
# on sale together for three months. Every key fact agrees because the three plots are
# identical; the body agrees because it is the developer's, not the plot's. What is left that
# only one unit has: its photographs and its order code. Two SEQUENTIAL postings are exempt —
# a re-post is one advert, and ninety bazos rows of one Slatinice house, one per day, have
# nothing else.
UNIT_EVIDENCE_IN_DEVELOPMENT: tuple[str, ...] = ("photo", "code")
# What M may count two of instead. A shared address point and an identical price path are
# strong, but a whole floor of one development shares both.
WIDER_EVIDENCE: tuple[str, ...] = UNIT_EVIDENCE + ("pin", "price_path")


def _stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def live_days(listing: Listing) -> float:
    """How long this advert was on sale, in days. Unknown ends read as zero, so an advert whose
    window cannot be measured never makes an overlap look small."""
    start = _stamp(listing.first_seen_at)
    end = _stamp(listing.inactive_at or listing.last_seen_at)
    if start is None or end is None:
        return 0.0
    return max(0.0, (end - start).total_seconds() / 86400.0)


def _slot(feats: Feats | None, name: str) -> float | None:
    if not feats:
        return None
    slot = feats.get(name)
    if not slot or len(slot) < 2 or not bool(slot[1]):
        return None
    return float(slot[0])  # type: ignore[arg-type]


def stored_headline_area(listing: Listing) -> float | None:
    """The size this advert is sold BY: the headline area, or the parcel when there is none."""
    if listing.area_m2 and float(listing.area_m2) > 0.0:
        return float(listing.area_m2)
    parcel = plot_area(listing)
    return float(parcel) if parcel else None


def body_headline_areas(listing: Listing, land: bool) -> frozenset[tuple[float, int]]:
    """The areas the BODY prints as this unit's, with the precision each was printed to."""
    scopes = {"unit", "land"} if land else {"unit"}
    return frozenset(
        (value, decimals)
        for value, decimals, scope in printed_areas(listing.description)
        if scope in scopes
    )


def _decimals_of(value: float) -> int:
    text = f"{value:.6f}".rstrip("0")
    return len(text.split(".", 1)[1]) if "." in text else 0


def degenerate_column(listing: Listing, land: bool) -> bool:
    """Is the stored headline a number the body says belongs to something else?

    bazos stores `area_m2 = 10.0` for a 76 m² apartment because it parsed `terasou o velikosti
    10 m²`, and every reader that goes through the stored column is then blind to the 76. The
    signature is exact and has to stay exact: the stored figure is one the body prints for an
    ACCESSORY, and the body also prints a headline several times larger. A house whose body
    names its 450 m² plot is not this — the column there is right and the plot is extra."""
    stored = stored_headline_area(listing)
    if stored is None:
        return False
    printed = body_headline_areas(listing, land)
    if not printed or max(value for value, _ in printed) < stored * DEGENERATE_RATIO:
        return False
    return any(abs(value - stored) <= 0.5 * 10.0 ** -decimals
               for value, decimals, scope in printed_areas(listing.description)
               if scope == "accessory")


def area_readings(listing: Listing, land: bool) -> frozenset[tuple[float, int]]:
    """Every TRUSTWORTHY reading of this advert's headline area, or empty when there is none.

    The stored column is dropped only where the body says it is not this unit's size — that is
    the only way past the `stated_areas` clamp, which hides the body's own number behind a
    window centred on the very column that is wrong."""
    printed = body_headline_areas(listing, land)
    stored = stored_headline_area(listing)
    if stored is None or degenerate_column(listing, land):
        return printed
    return printed | {(stored, _decimals_of(stored))}


def _areas_meet(left: frozenset[tuple[float, int]],
                right: frozenset[tuple[float, int]]) -> bool:
    return any(rounding_equal_values(a, da, b, db) for a, da in left for b, db in right)


def _has_decimal(readings: frozenset[tuple[float, int]]) -> bool:
    return any(decimals >= 1 for _, decimals in readings)


def decimals_decide(a: Listing, b: Listing, land: bool) -> bool:
    """E160: do the two BODIES both print the unit's size to a decimal?

    That, exactly, is the operator's rule: two printed DECIMALS that differ are two areas. The
    union of body and column is what lets 75,52 m² meet 75,64 m² — both portals store 76, and a
    0-decimal 76 meets every number that rounds to it — so where both sides have printed a
    decimal the column is a coarser copy of one of them and may not overrule the pair.

    Two printed INTEGERS are a different case and stay on the union: one idnes body prints
    `62 m²` for a flat whose column, and the other advert's body, both say 54, and the operator
    has confirmed that pair. An integer carries no claim about its own last digit."""
    left, right = body_headline_areas(a, land), body_headline_areas(b, land)
    return bool(left) and bool(right) and _has_decimal(left) and _has_decimal(right)


def deciding_areas(listing: Listing, land: bool, printed_decides: bool
                   ) -> frozenset[tuple[float, int]]:
    """What this advert's size is read off, given whether the printed decimals decide."""
    if printed_decides:
        printed = body_headline_areas(listing, land)
        if printed:
            return printed
    return area_readings(listing, land)


def area_demonstrated(a: Listing, b: Listing, printed_decides: bool = False) -> bool:
    """Both sides state a headline area and the two agree within rounding."""
    land = LAND_CATEGORY in (a.category_main, b.category_main)
    decides = printed_decides and decimals_decide(a, b, land)
    left = deciding_areas(a, land, decides)
    right = deciding_areas(b, land, decides)
    return bool(left) and bool(right) and _areas_meet(left, right)


def disposition_demonstrated(a: Listing, b: Listing) -> bool:
    """Both state the layout and it is the same one. Land has no layout to state."""
    if LAND_CATEGORY in (a.category_main, b.category_main):
        return True
    if a.disposition is None and b.disposition is None:
        return True
    return a.disposition is not None and a.disposition == b.disposition


def obec_demonstrated(a: Listing, b: Listing) -> bool:
    """Both adverts are resolved to a town and it is the same town."""
    left, right = a.location.obec_kod, b.location.obec_kod
    return left is not None and left == right


def overlap_days_local(a: Listing, b: Listing) -> float | None:
    """How long the two adverts were BOTH on sale. `indistinguishable.overlap_days` computes the
    same thing; it is repeated here because this module may not import that one."""
    starts = [_stamp(a.first_seen_at), _stamp(b.first_seen_at)]
    ends = [_stamp(a.inactive_at or a.last_seen_at), _stamp(b.inactive_at or b.last_seen_at)]
    if any(value is None for value in starts + ends):
        return None
    return max(0.0, (min(ends) - max(starts)).total_seconds() / 86400.0)  # type: ignore[operator]


def _sequential(a: Listing, b: Listing, settings: Settings, overlap: float | None) -> bool:
    """Were these two postings never really on sale together?

    A re-post boundary is a tail: the old row is still being sighted for a day or two after
    the new one appears, and that tail is a small FRACTION of both lives. Two Okružní garages
    first sighted seven minutes apart, the cheaper one dying two days later, also overlap by
    only 1.59 days — but that is its entire life, and they were on sale together throughout."""
    if overlap is None or overlap >= settings.demonstrate_price_colive_days:
        return False
    shortest = min(live_days(a), live_days(b))
    if shortest <= 0.0:
        return True
    return overlap <= settings.demonstrate_price_colive_fraction * shortest


def sequential_postings(a: Listing, b: Listing, settings: Settings) -> bool:
    """Were these two adverts never really on sale together? `_sequential` read from outside."""
    return _sequential(a, b, settings, overlap_days_local(a, b))


def price_demonstrated(
    a: Listing,
    b: Listing,
    settings: Settings,
    paths_agree: bool,
    overlap: float | None,
) -> bool:
    """One asking price, or one PATH. Two co-live prices are two prices.

    `paths_agree` and `overlap` are passed in rather than recomputed: `indistinguishable`
    already reads both for the `price` fact, and one reading per pair is the point."""
    if not (a.price and b.price and float(a.price) > 0.0 and float(b.price) > 0.0):
        return False
    cross = a.source is not None and b.source is not None and a.source != b.source
    # E160: EXACT, or on the other's recorded path. 5 % is what two portals carrying one order
    # look like — and also what a developer's next unit looks like, which is why it cannot be
    # the bar where identity is being CLAIMED rather than merely not contradicted.
    if settings.demonstrate_price_exact:
        tol = settings.demonstrate_price_exact_tol
    else:
        tol = PRICE_CROSS_TOL if cross else settings.d43_price_path_tol
    if rel_diff(float(a.price), float(b.price)) <= tol:
        return True
    if settings.demonstrate_price_rounding_aware and prices_round_equal(
            float(a.price), float(b.price)):
        return True
    # D57: the PATH is read at the same bar as the price. `price_paths_agree` runs at
    # `d43_price_path_tol` — 0.5 %, five times the exact bar and twenty-five times the Ráby
    # packages' 0.1 % — so leaving it alone readmits through the path every pair the exact bar
    # refuses, and the exactness is then a claim the engine does not keep.
    if settings.demonstrate_price_path_exact:
        from autodedup.indistinguishable import price_paths_agree

        paths_agree = price_paths_agree(a, b, tol) or (
            settings.demonstrate_price_rounding_aware
            and price_paths_round_equal(a, b))
    if paths_agree:
        return True
    # A cut between two SEQUENTIAL postings is one unit (the standing ruling).
    return _sequential(a, b, settings, overlap)


# The unit a printed number was ROUNDED to. `11,25 mil.` is 11,250,000 and its granularity is
# 10,000; `5 500 000` is granular to 100,000 and `5 499 000` to 1,000. Only powers of ten, and
# only up to a million — beyond that every asking price in the corpus is "round".
_PRICE_GRANULARITIES: tuple[float, ...] = (1e6, 1e5, 1e4, 1e3, 1e2, 1e1, 1.0)


def price_granularity(value: float) -> float:
    """The coarsest power of ten this amount is a whole multiple of."""
    for unit in _PRICE_GRANULARITIES:
        if abs(value / unit - round(value / unit)) < 1e-9:
            return unit
    return 1.0


def rendering_equal(left: float, right: float, cap: float) -> bool:
    """D57's arithmetic: is the coarser number the finer one written to FEWER digits?

    One number, two renderings, differs in GRANULARITY — `6 988 000` and its `6,98 mil.`, a
    897 m² parcel a second portal prints as `900`. Two numbers of the SAME granularity differ
    in nothing but value, and there is no rendering left to blame: `10 999 000` against
    `10 988 000`, `998` against `1 001`. The relative `cap` is a second condition and not the
    rule — a granularity artefact is a last-digit artefact, and 7,000,000 against 7,400,000
    agrees at a million while being a price cut.
    """
    if left == right:
        return True
    if left <= 0.0 or right <= 0.0 or rel_diff(left, right) > cap:
        return False
    coarse, fine = (left, right) if price_granularity(left) >= price_granularity(right) \
        else (right, left)
    unit = price_granularity(coarse)
    if unit <= 1.0 or unit == price_granularity(fine):
        return False
    return coarse / unit in (float(int(fine / unit)), float(round(fine / unit)))


# A granularity artefact is a last-digit artefact. Beyond this the two numbers are two prices
# however their trailing zeros fall — 7,000,000 and 7,400,000 agree at a million and are 5.7 %
# apart, which is a price cut, not a rendering.
PRICE_ROUNDING_CAP: float = 0.002


def prices_round_equal(left: float, right: float) -> bool:
    """`rendering_equal` at the price cap. S2's flat 0.2 % could not tell 0.114 % (a rendering)
    from 0.100 % (the next Ráby package); granularity can."""
    return rendering_equal(left, right, PRICE_ROUNDING_CAP)


def price_paths_round_equal(a: Listing, b: Listing) -> bool:
    """`prices_round_equal` over every amount the two adverts have ever printed."""
    from autodedup.indistinguishable import _price_points

    return any(prices_round_equal(left, right)
               for left in _price_points(a) for right in _price_points(b))


# A co-live price difference WIDER than this is not two units. D49 refused the co-live price
# contradiction on a mechanism: one Czech advert legitimately carries two prices for one unit at
# the same time — a Harrachov flat at a freehold 8,350,000 and a co-operative SHARE of 1,670,000,
# a 357 m² plot at an exekutorská-dražba 5,400 and an asking 17,000. Every one of those is a
# RATIO, not a margin. Three houses of one Hlubočky parcelling are 9,650,000 / 9,750,000 /
# 9,850,000 — 2 % apart, co-live for 82 days, one body. The small gap is the neighbouring unit.
PRICE_COLIVE_MAX_GAP: float = 0.20
# E164's floor: below this an advert is a headline, and a headline demonstrates nothing. It is
# `Settings.text_min_chars`, the same bar the engine uses to decide a body is readable at all.
RECOVER_MIN_BODY_CHARS: int = 200


def price_conflict(
    a: Listing,
    b: Listing,
    settings: Settings,
    paths_agree: bool,
    overlap: float | None,
) -> bool:
    """Two prices both adverts STATE, never reconciled, and too close to be two prices of one
    unit. The cluster-grain half of E157's price limb: the pairwise filter stops a promotion,
    and a group is built transitively, so without this the three Hlubočky houses still meet
    through a sixth portal whose 5 % cross-portal slack covers the 2 % between them."""
    if price_demonstrated(a, b, settings, paths_agree, overlap):
        return False
    if not (a.price and b.price):
        return False
    return rel_diff(float(a.price), float(b.price)) <= PRICE_COLIVE_MAX_GAP


# E164: which of the A-limb's refusals is a DISAGREEMENT and which is only a silence. On the
# two dev cohorts 87 % of arm M's A-limb losses are the second kind — no price on one side, an
# area no reader can make out — and 814 of 818 of them already carry unit-grade corroboration.
MISSING: str = "missing"
CONTRADICTION: str = "contradiction"


def _area_kind(a: Listing, b: Listing, printed_decides: bool) -> str:
    land = LAND_CATEGORY in (a.category_main, b.category_main)
    decides = printed_decides and decimals_decide(a, b, land)
    left = deciding_areas(a, land, decides)
    right = deciding_areas(b, land, decides)
    return MISSING if not left or not right else CONTRADICTION


def demonstration_gap(
    a: Listing,
    b: Listing,
    settings: Settings,
    paths_agree: bool,
    overlap: float | None,
) -> str | None:
    """(A) The first key fact the two adverts do not POSITIVELY agree on, or None."""
    return (demonstration_shortfall(a, b, settings, paths_agree, overlap) or (None, None))[0]


def demonstration_shortfall(
    a: Listing,
    b: Listing,
    settings: Settings,
    paths_agree: bool,
    overlap: float | None,
) -> tuple[str, str] | None:
    """(A) as `(fact, kind)`: WHICH key fact, and whether the two adverts disagree or one is
    silent. Nothing about the order or the tests changes — `demonstration_gap` is this function
    read for its first half, which is what every arm before W17 asked for."""
    printed = settings.demonstrate_area_printed_decides
    if not area_demonstrated(a, b, printed):
        return ("area", _area_kind(a, b, printed))
    if settings.demonstrate_require_disposition and not disposition_demonstrated(a, b):
        kind = MISSING if (a.disposition is None or b.disposition is None) else CONTRADICTION
        return ("disposition", kind)
    if not price_demonstrated(a, b, settings, paths_agree, overlap):
        priced = bool(a.price and b.price and float(a.price) > 0.0 and float(b.price) > 0.0)
        return ("price", CONTRADICTION if priced else MISSING)
    if settings.demonstrate_require_obec and not obec_demonstrated(a, b):
        known = a.location.obec_kod is not None and b.location.obec_kod is not None
        return ("obec", CONTRADICTION if known else MISSING)
    if settings.demonstrate_onesided:
        one_sided = onesided_fact(a, b, settings)
        if one_sided is not None:
            return (f"onesided_{one_sided}", CONTRADICTION)
    return None


def in_development(a: Listing, b: Listing) -> bool:
    """Does either advert use a developer's own vocabulary? `development.PROJECT_TERMS`, one
    spelling, so the K-B hold and this reading cannot drift apart. Imported inside the call
    because `development` reads a `Decision`, which reads this module."""
    from autodedup.development import PROJECT_TERMS

    for listing in (a, b):
        text = fold(listing.description or "")
        if any(term in text for term in PROJECT_TERMS):
            return True
    return False


# E162: the NARROW development context. `in_development` is `PROJECT_TERMS` on EITHER side, and
# the skeptic measured that at 41.8 % of all cohort-3 pairs — `novostavba` and `projekt` are how
# a Czech advert says "new" and "the plan", so a rule resting on it is not a context but a
# blanket. What makes a pair a development hazard is not the words but the SHAPE: the two
# adverts are written from one seller's template, they are on sale together, and the seller is
# selling units of a thing — which he admits by naming them, or by quoting a price list.
NEW_BUILD_TERMS: tuple[str, ...] = (
    "novostavb", "developer", "rezidenc", "etap", "kolaudac", "projekt",
)
# How much of the two bodies must be word-identical before they are one template. The body_align
# bar, deliberately: one number, one meaning.
TEMPLATE_MIN_RATIO: float = 0.60


def _new_build(listing: Listing) -> bool:
    text = fold(listing.description or "")
    return any(term in text for term in NEW_BUILD_TERMS)


def _names_a_unit(listing: Listing, settings: Settings) -> bool:
    """Does the advert print a whole unit CODE? `unit_designators` is deliberately not read —
    `byt č. 3` is how half the corpus writes a flat number and it is not a project marker."""
    from autodedup.text_facts import printed_unit_codes

    return bool(printed_unit_codes(listing.description, settings.d43_unit_codes_wide))


def development_context(a: Listing, b: Listing, settings: Settings) -> bool:
    """Are these two adverts inside one seller's development?

    `vocab` is `in_development` — `PROJECT_TERMS` on EITHER side — kept so the three readings
    can be measured against each other; the skeptic put it at 41.8 % of cohort-3 certain pairs,
    which is a blanket rather than a context.

    `narrow` is the reading W17 ships: the new-build vocabulary on BOTH sides, and the project
    NAMING a unit (a whole printed code on either side) or admitting to a price list. Measured
    on the certain duplicates of three cohorts it is 3.8 % / 1.1 % / 1.6 % where `vocab` is
    41.8 % / 27.7 % / 16.8 %, and the one-sided limb it gates costs 23 times less.

    `template` adds the shape the narrow reading leaves out — two co-live adverts written from
    one template. It is the strongest hazard signal and by far the widest: 3,574 of the 5,062
    both-vocabulary certain pairs of cohort 4 carry it, because a re-post carries a template
    too. Measured, named, not shipped."""
    mode = settings.development_context_mode
    if mode == "off":
        return False
    if mode == "vocab":
        return in_development(a, b)
    if not (_new_build(a) and _new_build(b)):
        return False
    if _names_a_unit(a, settings) or _names_a_unit(b, settings):
        return True
    from autodedup.text_facts import states_from_price

    if states_from_price(a.description) and states_from_price(b.description):
        return True
    if mode != "template":
        return False
    if _sequential(a, b, settings, overlap_days_local(a, b)):
        return False
    return _one_template(a, b, settings)


def _one_template(a: Listing, b: Listing, settings: Settings) -> bool:
    from autodedup.body_align import tokens

    left = tokens(a.description or "", settings.d43_body_align_heal)
    right = tokens(b.description or "", settings.d43_body_align_heal)
    if not left or not right:
        return False
    from difflib import SequenceMatcher

    matcher = SequenceMatcher(None, [t.text for t in left], [t.text for t in right],
                              autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return 2.0 * matched / (len(left) + len(right)) >= TEMPLATE_MIN_RATIO


def _printed_floors(listing: Listing) -> frozenset[int]:
    """Every storey the BODY prints as this unit's — `text_facts.printed_floors`, one spelling."""
    from autodedup.text_facts import printed_floors

    return printed_floors(listing.description)


def onesided_fact(a: Listing, b: Listing, settings: Settings) -> str | None:
    """E162: a unit-level fact one advert PRINTS and the other never states.

    Outside a development that is E12's missing datum and no reason to refuse anything: an
    advert that does not print its floor has contradicted nobody. INSIDE one it is the whole
    hazard — the S arm's residue is new-development unit twins whose code, floor or area is
    printed on one side only — so there the silent side fails closed.

    A side that prints a DIFFERENT value is not this rule's business: that is already a fact
    (`unit_code`, `floor`, `printed_area`), read in every mode and outside every context.

    Only what the BODY states is read. The stored floor COLUMN is not: one portal fills it and
    another does not, which is a difference between two portals and not a silence of the advert
    — it is one-sided on 3,304 of cohort 4's and 4,525 of the region's certain duplicates."""
    if not development_context(a, b, settings):
        return None
    from autodedup.text_facts import printed_unit_codes

    wide = settings.d43_unit_codes_wide
    for name, left, right in (
        ("code", printed_unit_codes(a.description, wide),
         printed_unit_codes(b.description, wide)),
        ("floor", _printed_floors(a), _printed_floors(b)),
    ):
        if bool(left) != bool(right):
            return name
    land = LAND_CATEGORY in (a.category_main, b.category_main)
    if bool(body_headline_areas(a, land)) != bool(body_headline_areas(b, land)):
        return "area"
    return None


def corroborations(a: Listing, b: Listing, feats: Feats | None, settings: Settings) -> list[str]:
    """Every piece of corroborating evidence this pair carries, named."""
    out: list[str] = []
    if (_slot(feats, "phash_tight_matches") or 0.0) >= 1.0:
        out.append("photo")
    if (_slot(feats, "containment_max") or 0.0) >= settings.corroboration_body_containment:
        out.append("body")
    if (_slot(feats, "ref_code_shared") or 0.0) >= 1.0:
        out.append("code")
    if ((_slot(feats, "same_exact_pin") or 0.0) >= 1.0
            or (_slot(feats, "same_ruian_adm_kod") or 0.0) >= 1.0):
        out.append("pin")
    if (_slot(feats, "price_path_event_match") or 0.0) >= 1.0:
        out.append("price_path")
    return out


def corroboration_warrant(
    a: Listing, b: Listing, feats: Feats | None, settings: Settings
) -> str | None:
    """(B) Why this pair is corroborated at unit grade, or None when it is not.

    Every mode answers `unit` first, so `unit` ⊆ `two_of` and `unit` ⊆ `development_only` hold
    whatever the data does — the ladder's nesting is a property of this function (E159)."""
    mode = settings.corroboration
    if mode == "off":
        return "off"
    found = corroborations(a, b, feats, settings)
    development = in_development(a, b)
    # A body shared by two SEQUENTIAL postings is one advert re-posted, whatever it stands in —
    # ninety bazos rows of one Slatinice house, one per day, are one unit and the body is all
    # they have. A body shared by two adverts on sale TOGETHER is the developer's template.
    grade = UNIT_EVIDENCE
    if development and not _sequential(a, b, settings, overlap_days_local(a, b)):
        grade = UNIT_EVIDENCE_IN_DEVELOPMENT
    unit = [name for name in grade if name in found]
    if unit:
        return unit[0]
    if mode == "unit":
        return None
    if mode == "two_of":
        return "+".join(found) if len(found) >= settings.corroboration_min else None
    return None if development else "outside_development"


def strong_corroboration(
    a: Listing, b: Listing, feats: Feats | None, settings: Settings
) -> str | None:
    """E164: evidence strong enough that a MISSING reading need not be demonstrated.

    (B)'s `unit` grade asks for ONE tight photo file, ONE order code or a contained body. That
    is the right bar for a pair whose key facts are all known and equal; it is not the bar for
    waiving one of those facts. What is admitted here is the sub-class the four cohorts show to
    be clean:

      * several tight non-catalogue photo FILES in common, with the galleries' weakest-but-one
        shared room above the interior floor where anything is known about it — a developer
        reuses the exterior render, not three interiors;
      * a shared rare order code, which is the seller's own name for ONE object;
      * a body one advert essentially IS — outside a development, where a shared body is the
        developer's template rather than this unit's (the Černovírské zahrady parcelling).

    A CONTRADICTION is never waived: two adverts that state two different areas are two
    statements, and no photograph outvotes a statement."""
    # The seller's own order code names ONE object and needs no prose beside it.
    if (_slot(feats, "ref_code_shared") or 0.0) >= 1.0:
        return "code"
    # Everything else needs both adverts to have SAID something. One ceskereality row of a
    # Slavonín house carries an empty body and no price at all, and recovering it on shared
    # photographs alone put it — and 39 cross-g7 pairs with it — inside cohort 3's confirmed
    # Františka Řeháka fusion. An advert that states nothing has demonstrated nothing.
    if min(len(a.description or ""), len(b.description or "")) < RECOVER_MIN_BODY_CHARS:
        return None
    photos = _slot(feats, "phash_tight_matches") or 0.0
    rooms = _slot(feats, "tag_room_clip_min2")
    if photos >= settings.demonstrate_recover_min_photos and (rooms is None or rooms >= 0.90):
        return f"photos:{photos:.0f}"
    contained = _slot(feats, "containment_max") or 0.0
    if (contained >= settings.demonstrate_recover_body_containment
            and not development_context(a, b, settings)
            and min(len(a.description or ""), len(b.description or "")) >= 300):
        return "body"
    return None
