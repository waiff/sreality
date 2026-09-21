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


def area_demonstrated(a: Listing, b: Listing) -> bool:
    """Both sides state a headline area and the two agree within rounding."""
    land = LAND_CATEGORY in (a.category_main, b.category_main)
    left, right = area_readings(a, land), area_readings(b, land)
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
    tol = PRICE_CROSS_TOL if cross else settings.d43_price_path_tol
    if rel_diff(float(a.price), float(b.price)) <= tol:
        return True
    if paths_agree:
        return True
    # A cut between two SEQUENTIAL postings is one unit (the standing ruling).
    return _sequential(a, b, settings, overlap)


# A co-live price difference WIDER than this is not two units. D49 refused the co-live price
# contradiction on a mechanism: one Czech advert legitimately carries two prices for one unit at
# the same time — a Harrachov flat at a freehold 8,350,000 and a co-operative SHARE of 1,670,000,
# a 357 m² plot at an exekutorská-dražba 5,400 and an asking 17,000. Every one of those is a
# RATIO, not a margin. Three houses of one Hlubočky parcelling are 9,650,000 / 9,750,000 /
# 9,850,000 — 2 % apart, co-live for 82 days, one body. The small gap is the neighbouring unit.
PRICE_COLIVE_MAX_GAP: float = 0.20


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


def demonstration_gap(
    a: Listing,
    b: Listing,
    settings: Settings,
    paths_agree: bool,
    overlap: float | None,
) -> str | None:
    """(A) The first key fact the two adverts do not POSITIVELY agree on, or None."""
    if not area_demonstrated(a, b):
        return "area"
    if settings.demonstrate_require_disposition and not disposition_demonstrated(a, b):
        return "disposition"
    if not price_demonstrated(a, b, settings, paths_agree, overlap):
        return "price"
    if settings.demonstrate_require_obec and not obec_demonstrated(a, b):
        return "obec"
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
