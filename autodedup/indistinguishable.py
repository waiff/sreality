"""D43: does any STATED fact tell these two adverts apart?

The operator's ruling of 2026-09-21 changes what a false merge IS. Two adverts are
INDISTINGUISHABLE when nothing either of them states separates the units, and merging two
indistinguishable adverts is the WANTED outcome even where a human or a judge called them
"different unit, same project". A false merge is now a merge ACROSS a distinguishing fact.

So this module answers one question and nothing else: which facts, if any, differ? It does not
score, does not decide a merge, and never guesses. Missing on either side is not a difference
(E12) — an advert that does not state a floor has not contradicted one that does.

WHERE THE NUMBERS COME FROM. Every tolerance below is the width of the PARSE NOISE measured on
this cohort's known duplicates — 1,320 dev-side pairs the operator, the gold judge or a
structural rule called one unit (`w14/truth/tolerance_measurement.json`). A fact is listed here
only when it fires far more often on the D43 hazard class (`same_building_different_unit` +
`same_project_different_unit`, one project two units) than on those known duplicates
(`w14/truth/fact_power2_dev.json`). Both rates are quoted per fact, dev side, so the cost of
each is on the record:

    fact                    fires on 1 unit   fires on 1 project, 2 units
    obec                         0.0 %                      0.0 %
    orientation                  0.0 %                     50.0 % (n=4)
    price, same portal >60 %     0.0 %                     12.5 %
    unit designator (E61)        0.0 %                (no labelled case)
    stated areas disjoint        0.4 %                     15.5 %
    disposition                  0.3 %              (vetoed before labelling)
    floorplan + rooms weak       0.7 %                     16.7 %
    floor                        1.1 % / 2.5 %             18.6 %
    area                         1.6 %                     32.8 %
    street                       1.7 %                      0.0 %
    plot area                    2.1 %                     78.6 % (gold)
    interior rooms               2.1 %                     36.8 %
    price, cross-portal          2.6 %                     27.0 %
    total floors                 4.2 %                     35.0 %

The `street` and `obec` rows look inert against the hazard class, and are supposed to: two
units of one project share a street. They are here because they separate the OTHER kind of
negative — a pair of unrelated adverts — where the street differs on 78 % of them.

REFUSED by the same measurement, and named so nobody re-proposes them:

  * `floorplan_conflict` ALONE — 6.6 % against 18.8 %, a ratio of 2.8. `features._tag_features`
    already measured it at AUC 0.474 in the hazard cell, which is chance. It survives here only
    in conjunction with a weak room match, where it costs 0.7 %.
  * a SAME-PORTAL price difference at a USEFUL tolerance — 10.5 % of known duplicates differ by
    more than 2 % on one portal, because an asking price moves and a re-post is still the same
    unit (the standing ruling that a re-post IS a duplicate says so directly). Only the gross
    form at 60 % survives, and it is free.
  * a floor gap of exactly one ACROSS portals — 45 % of cross-portal known duplicates carry it.
    Two portals disagree about `přízemí`; that is a vocabulary difference, not a fact.
  * the RÚIAN address code (4.2 %) and the house number (3.7 %) — one building has several
    entrances and two portals geocode one advert to two of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from autodedup.body_align import aligned_difference, rounding_equal_values
from autodedup.dataset import Listing
from autodedup.demonstrate import (
    area_readings,
    body_headline_areas,
    decimals_decide,
    rendering_equal,
    sequential_postings,
)
from autodedup.features import (
    STREET_GRAIN_RANK,
    haversine_m,
    plot_area,
    plot_reading,
    plot_residue,
    rel_diff,
)
from autodedup.floor_convention import (
    convention_ambiguous,
    convention_known,
    floor_gap,
    joint_convention_shift,
    same_camp,
)
from autodedup.guards import LAND_CATEGORY, area_rel_diff, area_relation
from autodedup.settings import Settings
from autodedup.structural_truth import areas_disjoint
from autodedup.text_facts import (
    accessory_designators,
    address_block_key,
    capacity_counts,
    fold,
    ground_or_upper,
    leading_area,
    offered_room_counts,
    orientations,
    parcel_numbers,
    parcel_numbers_wider,
    parcel_table,
    prose_streets,
    printed_floors,
    stated_areas,
    stated_unit_counts,
    streets_agree,
    subject_floors,
    printed_unit_codes,
    unit_designators,
)
from toolkit.room_taxonomy import category_main_compatible

# The parcel: the engine's own `plot_area_exact` bar. Below it two portals printed one parcel.
PLOT_TOL: float = 0.02
# The asking price, ACROSS portals. p95 of the cross-portal difference on known duplicates is
# 4.3 % and p90 is 0.0 %, so 5 % is the first round number above what one order looks like when
# two portals carry it.
PRICE_CROSS_TOL: float = 0.05
# On ONE portal an asking price MOVES, and a re-post at a new price is still the same unit
# (the standing ruling says so), so the same statistic there is p90 9.2 % / p95 20 % / p99 43 %
# and no useful tolerance exists. What survives is the gross form: the widest gap any known
# duplicate shows on one portal is 55 %, so beyond 60 % the two numbers are not one asking
# price at two moments. It is worth reading because it is free — 0 of 523 known duplicates —
# and it is the only thing that separates a 2.5M nebytový prostor from a 9.9M dům on bazos.
PRICE_SAME_SOURCE_TOL: float = 0.60
# The weakest-but-one shared room, below which two galleries are not photographs of one home.
# `features._tag_features` measures `tag_room_clip_min2` at AUC 0.820 in the hazard cell — the
# best signal there is — and 0.90 is where it costs 2.1 % of known duplicates.
ROOM_CLIP_FLOOR: float = 0.90
# The floor plan, read only together with a weak room match: the bare conflict is chance.
FLOORPLAN_ROOM_CLIP_FLOOR: float = 0.90

FLAT_CATEGORY: str = "byt"

# The three readings, named so a caller cannot pass a boolean and mean the wrong one.
PROMOTE: str = "promote"
GATE: str = "gate"
CLUSTER: str = "cluster"
MODES: tuple[str, ...] = (PROMOTE, GATE, CLUSTER)

# The only feature slots this module reads. A caller that has to carry a feature row for every
# pair (the cluster relation does) carries these three and nothing else.
FEATURE_SLOTS: tuple[str, ...] = (
    "floorplan_conflict",
    "tag_room_clip_min2",
    "phash_tight_matches",
)

# Every name this module can return, so a caller can tabulate without discovering them.
FACT_NAMES: tuple[str, ...] = (
    "category_type",
    "category_main",
    "area",
    "stated_area",
    "disposition",
    "floor",
    "total_floors",
    "plot_area",
    "price",
    "obec",
    "street",
    "orientation",
    "unit_designator",
    "parcel",
    "accessory",
    "extent",
    "two_unit",
    "floorplan",
    "interior",
    "body_align",
    "street_prose",
    "obec_prose",
    "printed_area",
    "unit_code",
    "prose_floor",
    "subject_floor",
    "storey_word",
    "unit_count",
    "offer_area",
)


@dataclass(frozen=True, slots=True)
class Fact:
    """One difference, with both sides' value so a human can adjudicate the claim."""

    name: str
    left: str
    right: str

    def as_json(self) -> dict[str, str]:
        return {"fact": self.name, "left": self.left, "right": self.right}


Feats = Mapping[str, Sequence[object]]


def _present(feats: Feats | None, name: str) -> float | None:
    """The engine's feature value when it was computed, else None (absent is not a fact)."""
    if not feats:
        return None
    slot = feats.get(name)
    if not slot or len(slot) < 2 or not bool(slot[1]):
        return None
    return float(slot[0])  # type: ignore[arg-type]


def _price_points(listing: Listing) -> list[float]:
    """Every amount this advert has ever printed, the current one included (E134/N2)."""
    points: list[float] = []
    for _stamp, price in listing.price_history or ():
        if price is not None and float(price) > 0.0:
            points.append(float(price))
    if listing.price and float(listing.price) > 0.0:
        points.append(float(listing.price))
    return points


def price_paths_agree(a: Listing, b: Listing, tol: float) -> bool:
    """True when the two price PATHS ever name the same amount within `tol` (E134/N2).

    A dead advert holds the price of the day it died while the live one moved on, so two
    current numbers are two moments, not two statements. Paths name a common amount on 90.4 %
    of known duplicates against 51.4 % of the one-project-two-units hazard class.
    """
    left, right = _price_points(a), _price_points(b)
    if not left or not right:
        return False
    return any(rel_diff(x, y) <= tol for x in left for y in right)


def _stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def overlap_days(a: Listing, b: Listing) -> float | None:
    """How long the two adverts were BOTH on sale, in days, or None when a stamp is missing.

    E144: the co-live limb reads a DURATION, not a boolean. Two windows that merely touch are a
    re-post boundary — the old advert's last sighting and the new one's first sighting hours
    apart — and 38 of the 63 raw price contradictions g8 carried were exactly that.
    """
    starts = [_stamp(a.first_seen_at), _stamp(b.first_seen_at)]
    ends = [_stamp(a.inactive_at or a.last_seen_at), _stamp(b.inactive_at or b.last_seen_at)]
    if any(value is None for value in starts + ends):
        return None
    span = min(ends) - max(starts)  # type: ignore[type-var, operator]
    return max(0.0, span.total_seconds() / 86400.0)


def _co_live(a: Listing, b: Listing, min_days: float) -> bool:
    """Simultaneously on sale for at least `min_days` — 0 keeps `_windows_overlap` exactly."""
    if min_days <= 0.0:
        return _windows_overlap(a, b)
    days = overlap_days(a, b)
    return days is not None and days >= min_days


def _windows_overlap(a: Listing, b: Listing) -> bool:
    """Both adverts were on sale at the same time, read off the export's own stamps."""
    starts = (a.first_seen_at, b.first_seen_at)
    ends = (a.inactive_at or a.last_seen_at, b.inactive_at or b.last_seen_at)
    if any(value is None for value in starts + ends):
        return False
    return starts[0] <= ends[1] and starts[1] <= ends[0]  # type: ignore[operator]


def _pins_together(a: Listing, b: Listing, within_m: float | None) -> bool:
    """Two street names, one place: the pins are metres apart (E138).

    Read only under E16's own precision gate — below street grain the coordinate is an
    administrative centroid, and the distance between two town halls says nothing about two
    flats. A corner building carries two street names and one point.
    """
    if within_m is None:
        return False
    left, right = a.location, b.location
    if not (left.has_point() and right.has_point()):
        return False
    if left.granularity_rank is None or right.granularity_rank is None:
        return False
    if (left.granularity_rank < STREET_GRAIN_RANK
            or right.granularity_rank < STREET_GRAIN_RANK):
        return False
    return haversine_m(float(left.lat), float(left.lon),
                       float(right.lat), float(right.lon)) <= within_m


def _at_street_grain(listing: Listing) -> bool:
    rank = listing.location.granularity_rank
    return rank is not None and rank >= STREET_GRAIN_RANK


def tight_photo_match(feats: Feats | None) -> bool:
    """At least one tight NON-CATALOGUE frame in common (E9 already subtracted the stock)."""
    return (_present(feats, "phash_tight_matches") or 0.0) >= 1.0


def _same_feed(a: Listing, b: Listing, mode: str, unknown_closed: bool = False) -> bool:
    """E145: are these two adverts known to carry the same ground-floor convention?

    `portal` is g7/g8's answer — one portal, one convention. The data refuses it: among
    same-portal KNOWN duplicates a one-storey gap runs at 7.5 % on sreality and 23.5 % on
    bazos, because the number is written by the BROKER whose feed the portal republishes.
    `broker` therefore asks for the feed itself.

    E154: and it must fail CLOSED. As shipped, an advert with no broker key was "never known
    to share a feed with anybody", which SILENCES the same-portal one-storey fact — and bazos,
    bezrealitky and maxima are 100 % null, so floors 3 and 4 of one bezrealitky new-build
    fused. A null key is UNKNOWN, and unknown is not a licence: with `unknown_closed` the fact
    stands unless both keys are known AND different.
    """
    if a.source is None or a.source != b.source:
        return False
    if mode != "broker":
        return True
    if unknown_closed:
        return not (a.broker_key is not None and b.broker_key is not None
                    and a.broker_key != b.broker_key)
    return a.broker_key is not None and a.broker_key == b.broker_key


def _feed_known(a: Listing, b: Listing) -> bool:
    """Do both adverts name the feed they came from?"""
    return a.broker_key is not None and b.broker_key is not None


def _prices_meet(a: Listing, b: Listing, tol: float) -> bool:
    """One asking price, written twice."""
    if not (a.price and b.price and float(a.price) > 0.0 and float(b.price) > 0.0):
        return False
    return rel_diff(float(a.price), float(b.price)) <= tol


def headline_area(listing: Listing) -> float | None:
    """The size this advert is sold BY: the headline area, or the parcel when there is none."""
    if listing.area_m2 and float(listing.area_m2) > 0.0:
        return float(listing.area_m2)
    parcel = plot_area(listing)
    return float(parcel) if parcel else None


def _stated_areas_meet(a: Listing, b: Listing, tol: float) -> bool:
    """Both BODIES print a common floor area — the stored gap is then a basis difference."""
    left = stated_areas(a.description, a.area_m2)
    right = stated_areas(b.description, b.area_m2)
    if not left or not right:
        return False
    return any(rel_diff(x, y) <= tol for x in left for y in right)


def two_unit_signature(a: Listing, b: Listing, settings: Settings | None = None) -> bool:
    """E143: area AND price both moved, so these are two units rather than one at two moments.

    An advert has one area and one price at any moment. A re-post carries a NEW price at the
    SAME area; a second portal carries the same price at a ROUNDED area; a developer's next
    unit carries both, one or two per cent apart — which is exactly the window the engine's
    3 % / 5 % / 60 % tolerances cannot see into.
    """
    cfg = settings or Settings()
    area_a, area_b = headline_area(a), headline_area(b)
    if area_a is None or area_b is None:
        return False
    if not (a.price and b.price and float(a.price) > 0.0 and float(b.price) > 0.0):
        return False
    if rel_diff(area_a, area_b) <= cfg.d43_two_unit_area_tol:
        return False
    if rel_diff(float(a.price), float(b.price)) <= cfg.d43_two_unit_price_tol:
        return False
    if price_paths_agree(a, b, cfg.d43_price_path_tol):
        return False
    # E165: "an advert has one area and one price AT ANY MOMENT" is the whole argument, and it
    # says nothing about two adverts that were never on sale at one moment. A re-post at a cut
    # price whose area one portal re-parsed carries both halves of the signature and is one unit
    # by the standing ruling — four of the region skeptic's twelve named false splits are
    # exactly that. A co-live pair is untouched: there the two numbers ARE simultaneous.
    if cfg.d43_two_unit_requires_colive and sequential_postings(a, b, cfg):
        return False
    return not _stated_areas_meet(a, b, cfg.d43_two_unit_stated_tol)


def printed_area_conflict_cfg(a: Listing, b: Listing, settings: Settings | None = None
                              ) -> tuple[str, str] | None:
    """`printed_area_conflict` with E160's reading of which numbers decide."""
    cfg = settings or Settings()
    land = LAND_CATEGORY in (a.category_main, b.category_main)
    if not body_headline_areas(a, land) or not body_headline_areas(b, land):
        return None
    if not (cfg.d43_printed_area_decimals_decide and decimals_decide(a, b, land)):
        return printed_area_conflict(a, b)
    # E160: where both bodies PRINT, the printed figures decide. `75,52` against `75,64` is two
    # flats of one Chotěšov row, and the only thing that made them meet was the stored 76 both
    # portals rounded to — a coarser copy of one of the two numbers, overruling both.
    left, right = body_headline_areas(a, land), body_headline_areas(b, land)
    if any(rounding_equal_values(x, dx, y, dy) for x, dx in left for y, dy in right):
        return None
    return (str(sorted(value for value, _ in left)),
            str(sorted(value for value, _ in right)))


def printed_area_conflict(a: Listing, b: Listing) -> tuple[str, str] | None:
    """E153: both adverts state a headline area and no two readings agree within rounding.

    The readings are the body's SCOPED figures (the terrace's size is the terrace's, a
    bedroom's is the bedroom's) together with the stored column, which is dropped only where
    the body says it is not this unit's size. Keeping the column in is what tells `47,6 m²`
    against `46,6 m²` — two portals measuring one flat, both storing 47 — from `76,1` against
    `77,8`, where bazos stores the terrace for both and the column has nothing to say. A body
    that prints two numbers for one unit (`užitná 51 m² / podlahová 55 m²`) meets the other
    side on whichever it shares, which is the basis difference E143 already carries."""
    land = LAND_CATEGORY in (a.category_main, b.category_main)
    # Both BODIES must print one, or this reader has nothing to say. A stored column alone is
    # already the `area` fact at the tolerance a stored column deserves; re-reading it here at
    # rounding precision would refuse every pair one portal rounded 58,9 down to 58.
    if not body_headline_areas(a, land) or not body_headline_areas(b, land):
        return None
    left, right = area_readings(a, land), area_readings(b, land)
    if any(rounding_equal_values(x, dx, y, dy) for x, dx in left for y, dy in right):
        return None
    return (str(sorted(value for value, _ in left)),
            str(sorted(value for value, _ in right)))


def prose_street_conflict(a: Listing, b: Listing) -> tuple[str, str] | None:
    """E151: both BODIES name a street and they name no street in common.

    Prose against prose only. The resolved `street_key` has its own fact (`street`, at
    `d43_street_min_distance_m`); reading it here as well turns every gap in the geocoder into
    a refusal — one Čelakovského advert whose neighbour's key resolved elsewhere, one bazos row
    whose body was the only side that named anything at all."""
    left, right = prose_streets(a.description), prose_streets(b.description)
    if not left or not right or streets_agree(left, right):
        return None
    return (",".join(sorted(left)), ",".join(sorted(right)))


def prose_obec_conflict(a: Listing, b: Listing) -> bool:
    """E152: each body PRINTS its own town's name and neither prints the other's.

    E135 suppresses the resolved obec below street grain because a village is routinely
    advertised under its district town — but that ambiguity is in the GEOCODE, not in the
    prose. An advert that writes "Oplocany u Tovačova" and never writes "Droždín" has stated
    which town it is in."""
    left, right = a.location.obec_name, b.location.obec_name
    if not left or not right or a.location.obec_kod == b.location.obec_kod:
        return False
    if a.location.obec_kod is None or b.location.obec_kod is None:
        return False
    body_a, body_b = fold(a.description or ""), fold(b.description or "")
    name_a, name_b = fold(left), fold(right)
    return (name_a in body_a and name_b in body_b
            and name_a not in body_b and name_b not in body_a)


def _set_conflict(left: frozenset[str] | set[str], right: frozenset[str] | set[str]) -> bool:
    """Two non-empty printed sets that name nothing in common. A subset is NOT a conflict —
    an advert also names the access road, the neighbour's plot and the extra parking space."""
    return bool(left and right and not (left & right))


def selected_parcels(listing: Listing, settings: Settings) -> set[str]:
    """E182: the parcels THIS advert sells, not every parcel its body prints.

    A body that prints the seller's whole catalogue says which row is its own, because each row
    carries an area and a price and the advert carries the same two numbers. One Morašice
    sreality advert lists seven parcels and is itself the 1,458 m² / 3,459,000 Kč row, parcels
    274/8 + 274/13; the idnes advert of the NEXT plot prints only `číslo pozemku: 274/9 +
    274/14`. Read as sets those two share 274/9 and never contradict; read by the row they are
    two parcels of one parcelling.

    Selection must be UNIQUE — two rows at one price and one area are a catalogue this reader
    cannot resolve, and it then falls back to the union, which is what every earlier arm read.
    """
    if not settings.d43_parcel_table:
        return parcel_numbers(listing.description, settings.d43_parcel_forms_wide)
    rows = parcel_table(listing.description)
    own_area = headline_area(listing)
    own_price = float(listing.price) if listing.price else None
    matched = [
        row for row in rows
        if (own_area is not None and abs(row[1] - own_area) <= 0.5)
        or (own_price is not None and abs(row[2] - own_price) <= 0.5)
    ]
    if len(matched) == 1:
        return set(matched[0][0])
    return parcel_numbers_wider(listing.description)


# The categories whose parcel IS the object. A flat's building sits on a plot too, and that
# plot is the building's, not the unit's — reading it exactly there would split every pair of
# flats two portals resolved to two entrances of one block.
PLOT_EXACT_CATEGORIES: frozenset[str] = frozenset({"dum", LAND_CATEGORY})


def _plot_conflict(
    a: Listing, b: Listing, settings: Settings, is_land: bool
) -> tuple[str, str] | None:
    """E182: two parcel areas that are not one parcel.

    Two readings change here. The TOLERANCE: 2 % is what two portals printing one parcel look
    like, and it is also what the next plot of a parcelling looks like — Ráby's packages are
    981, 998 and 1,001 m², 0.3 % apart, each mirrored unchanged on four portals. Where the
    object IS the parcel (a house or a plot) the number is read exactly.

    The CARRIER: `plot_area` blanks a source whose parser truncates thousands, which is right
    for a scored feature and wrong for a fact. A truncation removes leading digit groups and
    never the last three, so two untrusted numbers still prove two parcels when their residues
    differ — two ceskereality adverts of Rezidence Loučná at 227 and 191 m², one catalogue
    price, 82 days together. Where a residue AGREES nothing is claimed: 870 may be 5,870.
    """
    parcel_is_the_object = {a.category_main, b.category_main} <= PLOT_EXACT_CATEGORIES
    exact = settings.d43_plot_area_exact and parcel_is_the_object

    def apart(left: float, right: float) -> bool:
        if not exact:
            return rel_diff(left, right) > PLOT_TOL
        # Two portals measuring ONE parcel differ in the last digit or in how coarsely they
        # print it: 1,667 against 1,668, or 897 against a rendered 900. Two parcels of one
        # parcelling differ by more, at one granularity: Ráby's 998 against 1,001.
        if abs(left - right) <= settings.d43_plot_exact_abs:
            return False
        return not rendering_equal(left, right, PLOT_TOL)

    if not settings.d43_plot_truncation_residue or not parcel_is_the_object:
        plot_a, plot_b = plot_area(a), plot_area(b)
        if plot_a and plot_b and apart(plot_a, plot_b):
            return (str(plot_a), str(plot_b))
        return None
    left, right = plot_reading(a), plot_reading(b)
    if left is None or right is None:
        return None
    (value_a, trusted_a), (value_b, trusted_b) = left, right
    if trusted_a and trusted_b:
        return (str(value_a), str(value_b)) if apart(value_a, value_b) else None
    # A truncated number has lost its leading groups, so only the residue can be compared —
    # and only for a house or a plot, where the parcel IS the object. One ceskereality
    # `estate_area` of 920 against a 14,788 m² commercial parcel is not a truncation of it,
    # it is a different measurement the portal put in the same column.
    residue_a, residue_b = plot_residue(value_a), plot_residue(value_b)
    if abs(residue_a - residue_b) > settings.d43_plot_exact_abs:
        return (f"{value_a}(residue {residue_a})", f"{value_b}(residue {residue_b})")
    return None


def _offer_area_conflict(
    a: Listing, b: Listing, settings: Settings, is_land: bool
) -> tuple[str, str] | None:
    """E186: one advert leads with a size the other never prints at all.

    `printed_area` compares SETS, so the advert of one third of a parcel meets the advert of
    the whole: it names the 2,195 m² parcel while explaining that its own 732 m² will be cut
    from it, and the shared number decides. What an advert LEADS with is what it sells.

    Order alone is not a fact, and that is what makes this safe. One Jablonec hotel is carried
    by idnes under a headline naming the 14,788 m² plot and by two other portals under the
    2,900 m² floor area, and both bodies print BOTH numbers — one advert, two orders of
    presentation. The fact is a leading figure the other body never states.

    A body carrying a parcel CATALOGUE is skipped: its first row is the seller's first plot,
    not this advert's, and `selected_parcels` is the reader for that shape.
    """
    if parcel_table(a.description) or parcel_table(b.description):
        return None
    scopes = frozenset({"unit", "land"}) if is_land else frozenset({"unit"})
    lead_a = leading_area(a.description, scopes)
    lead_b = leading_area(b.description, scopes)
    if lead_a is None or lead_b is None:
        return None
    if rounding_equal_values(lead_a[0], lead_a[1], lead_b[0], lead_b[1]):
        return None
    small, large = sorted((lead_a[0], lead_b[0]))
    if small <= 0.0 or large / small < settings.d43_offer_area_min_ratio:
        return None
    printed_a = body_headline_areas(a, is_land)
    printed_b = body_headline_areas(b, is_land)
    unstated = (
        not any(rounding_equal_values(lead_a[0], lead_a[1], value, decimals)
                for value, decimals in printed_b)
        or not any(rounding_equal_values(lead_b[0], lead_b[1], value, decimals)
                   for value, decimals in printed_a)
    )
    if not unstated:
        return None
    return (str(lead_a[0]), str(lead_b[0]))


def _rounded_floors(a: Listing, b: Listing, settings: Settings, gap: int) -> bool:
    """E180: is a `gap`-storey difference a difference the convention cannot explain?

    Two storeys is a fact anywhere and always was. ONE storey is the ground-floor vocabulary
    only ACROSS camps; inside one camp — and a portal is always inside its own — there is no
    vocabulary left to blame, so the gap is a storey. `d43_floor_within_camp_colive` keeps the
    excuse for two postings that were never on sale together: there the gap is one portal's
    parse drifting between re-posts, and 29 bazos re-posts of one Dašice rent advert do exactly
    that. Two adverts LIVE TOGETHER have no such excuse.
    """
    if abs(gap) >= 2:
        return True
    if abs(gap) != 1 or not settings.d43_floor_within_camp:
        return False
    if not same_camp(settings.floor_camps, a.source, b.source):
        return False
    if settings.d43_floor_within_camp_colive and sequential_postings(a, b, settings):
        return False
    return True


def _price_sequential_path(
    a: Listing, b: Listing, feats: Feats | None, settings: Settings
) -> bool:
    """E185: a price MOVE between two postings that were never on sale together.

    The standing ruling says a re-post at a new price is the same unit, and `price_demonstrated`
    has always honoured it — but the `price` FACT never did, so a sreality advert re-listed at
    13,700,000 after 14,500,000 is split from its own re-post and from every portal that copied
    either number. The ruling is only safe with an identity beside it: the two postings must not
    overlap, they must agree on area, disposition and storey, and the bodies or the photographs
    must be the same advert's. A CO-LIVE pair is untouched — there the two prices are
    simultaneous, which is D49's whole mechanism.
    """
    if not settings.d43_price_sequential_path:
        return False
    if not sequential_postings(a, b, settings):
        return False
    if settings.d43_price_sequential_same_feed and not _same_feed(a, b, "broker", True):
        return False
    if a.disposition is not None and b.disposition is not None and a.disposition != b.disposition:
        return False
    if a.floor is not None and b.floor is not None and a.floor != b.floor:
        return False
    gap = area_rel_diff(a.area_m2, b.area_m2)
    if gap is not None and gap > 0.0:
        return False
    photos = _present(feats, "phash_tight_matches") or 0.0
    contained = _present(feats, "containment_max") or 0.0
    return (photos >= settings.d43_price_sequential_min_photos
            or contained >= settings.d43_price_sequential_containment)


def offered_extent(a: Listing, b: Listing, settings: Settings | None = None) -> tuple[str, str] | None:
    """E142: the two adverts offer a different QUANTITY of the same kind of thing.

    Three spellings, one fact. The capacity and room-count limbs need the conjunction that
    isolates a real product tier from a template's prose: the prices must differ and the two
    adverts must have been on sale at the same time. The parcel limb needs neither — an
    enumerated inventory one advert offers strictly more of is a different offer outright.
    """
    cfg = settings or Settings()
    parcels_a = parcel_numbers(a.description, cfg.d43_parcel_forms_wide)
    parcels_b = parcel_numbers(b.description, cfg.d43_parcel_forms_wide)
    if parcels_a and parcels_b and parcels_a != parcels_b and (
        parcels_a < parcels_b or parcels_b < parcels_a
    ):
        return (f"parcels={sorted(parcels_a)}", f"parcels={sorted(parcels_b)}")
    if cfg.d43_offered_extent_requires_price_gap:
        priced = (a.price and b.price and float(a.price) > 0.0 and float(b.price) > 0.0
                  and rel_diff(float(a.price),
                               float(b.price)) > cfg.d43_offered_extent_price_tol)
        if not priced or not _co_live(a, b, cfg.d43_price_colive_min_overlap_days):
            return None
    for reader, label in ((capacity_counts, "capacity"), (offered_room_counts, "rooms")):
        left, right = reader(a.description), reader(b.description)
        if left and right and left != right:
            return (f"{label}={sorted(left)}", f"{label}={sorted(right)}")
    return None


def distinguishing_facts(
    a: Listing,
    b: Listing,
    feats: Feats | None = None,
    settings: Settings | None = None,
    mode: str = PROMOTE,
) -> list[Fact]:
    """Every stated fact that differs between two adverts. Empty list = indistinguishable.

    `feats` is one row of the engine's own feature vector, and is the only way the image facts
    can be read — this module computes no image evidence of its own. Passing None simply drops
    those two facts, which is the correct reading of an advert whose photographs nobody paired.

    THE THREE READINGS (E136/E138). `promote` is strict: merging on the ABSENCE of a fact is
    the one place the engine has no positive evidence to fall back on. `gate` is permissive —
    it is about to demote a merge the engine already certified, so it does not demote on a
    difference that is INFERRED rather than stated, nor on one a known vocabulary or geocode
    ambiguity explains. `cluster` is `gate` with the image facts PUT BACK: the invariant is the
    only thing standing between a development and one big group, and dropping its best
    hazard-cell discriminator costs four bad groups where dropping it at the gate costs none.
    """
    cfg = settings or Settings()
    out: list[Fact] = []

    def add(name: str, left: object, right: object) -> None:
        out.append(Fact(name, str(left), str(right)))

    # The standing rulings first: these are not tolerances, they are walls.
    if (a.category_type is not None and b.category_type is not None
            and a.category_type != b.category_type):
        add("category_type", a.category_type, b.category_type)
    # `category_main_compatible`, not raw inequality: dům <-> komerční is the one sanctioned
    # cross-type (rule #15), and the operator has confirmed 8 merges across it in this cohort.
    if not category_main_compatible(a.category_main, b.category_main):
        add("category_main", a.category_main, b.category_main)

    # E136/N3: promotion reads the strict three-way verdict; the gate reads the wider bar the
    # merge already cleared, so a parse gap in the 3-8 % band cannot split a certified merge.
    lenient = mode in (GATE, CLUSTER)
    gate_area_tol = cfg.d43_gate_area_tol if lenient else None
    if gate_area_tol is not None:
        gap = area_rel_diff(a.area_m2, b.area_m2)
        if gap is not None and gap > gate_area_tol:
            add("area", a.area_m2, b.area_m2)
    elif area_relation(a.area_m2, b.area_m2, cfg) not in ("support", "unknown"):
        add("area", a.area_m2, b.area_m2)

    areas_a = stated_areas(a.description, a.area_m2)
    areas_b = stated_areas(b.description, b.area_m2)
    if areas_disjoint(areas_a, areas_b):
        add("stated_area", sorted(areas_a), sorted(areas_b))

    is_land = LAND_CATEGORY in (a.category_main, b.category_main)
    if (not is_land and a.disposition is not None and b.disposition is not None
            and a.disposition != b.disposition):
        add("disposition", a.disposition, b.disposition)

    # E133/N1: the floor gap with the portal's ground-floor convention taken out. Where the
    # camps place BOTH sources the residual gap is real and any of it is a fact; where a camp
    # is unknown (bazos posts both ways) a one-floor gap stays the vocabulary difference it is
    # on 45 % of cross-portal known duplicates. With no camp table this is g7's rule exactly.
    reads = cfg.floor_camps_reads
    gap = floor_gap(cfg.floor_camps if reads != "joint" else None,
                    a.source, a.floor, b.source, b.floor)
    if gap is not None:
        same_feed = _same_feed(a, b, cfg.floor_same_source_feed,
                               cfg.floor_feed_unknown_closed)
        # E154, the other half: with the feed UNKNOWN the one-storey gap is read closed, and a
        # storey typed two ways then looks exactly like two flats. The asking price is what
        # separates them. One 131 m² 4+1 in a Jablonec vila is re-posted on ceskereality at
        # 6,988,000 and 6,980,000 with its storey written both 1. NP and 2. NP; THE FIZZ's
        # floors 5 and 6 are 11,290 and 12,025. Neither signal is a fact alone; together they
        # are, and it is the price that makes the difference a unit's rather than a typist's.
        if (same_feed and abs(gap) == 1 and cfg.floor_feed_unknown_closed
                and not _feed_known(a, b) and _prices_meet(a, b, cfg.d43_price_path_tol)):
            same_feed = False
        strict = reads == "strict" and convention_known(cfg.floor_camps, a.source, b.source)
        within = _rounded_floors(a, b, cfg, gap)
        if (gap != 0) if strict else (within or (abs(gap) == 1 and same_feed)):
            add("floor", a.floor, b.floor)

    if a.total_floors is not None and b.total_floors is not None:
        delta_total = abs(a.total_floors - b.total_floors)
        joint = reads != "off" and joint_convention_shift(
            cfg.floor_camps, a.source, a.floor, a.total_floors,
            b.source, b.floor, b.total_floors)
        # E138: one storey across a boundary the camps cannot place is the ground-floor
        # ambiguity again — the same slack `floor` already carries across every portal pair.
        ambiguous = (lenient and cfg.d43_gate_total_floors_slack and delta_total == 1
                     and convention_ambiguous(cfg.floor_camps, a.source, b.source))
        if delta_total and not joint and not ambiguous:
            add("total_floors", a.total_floors, b.total_floors)

    plot_conflict = _plot_conflict(a, b, cfg, is_land)
    if plot_conflict is not None:
        add("plot_area", plot_conflict[0], plot_conflict[1])

    if a.price and b.price and a.price > 0 and b.price > 0:
        price_gap = rel_diff(float(a.price), float(b.price))
        cross = a.source is not None and b.source is not None and a.source != b.source
        over = price_gap > (PRICE_CROSS_TOL if cross else PRICE_SAME_SOURCE_TOL)
        if cfg.d43_price_path:
            agree = price_paths_agree(a, b, cfg.d43_price_path_tol)
            # E134: the momentary gap is excused by an agreeing path; a CONTRADICTION — two
            # adverts on sale at the same time that never named one another's price — is a
            # fact at the cross-portal bar whether or not they share a portal.
            colive_side = not cfg.d43_price_colive_same_source_only or not cross
            contradiction = (cfg.d43_price_colive_contradiction and not agree and colive_side
                             and price_gap > PRICE_CROSS_TOL
                             and _co_live(a, b, cfg.d43_price_colive_min_overlap_days))
            # E185: a move between two postings that were never on sale together is one path.
            moved = _price_sequential_path(a, b, feats, cfg)
            if ((over and not agree) or contradiction) and not moved:
                add("price", a.price, b.price)
        elif over:
            add("price", a.price, b.price)

    # WHERE the advert says the unit is. Only the two coarse keys are read. The finer ones were
    # measured and refused: a RÚIAN address code differs on 4.2 % of known duplicates and a house
    # number on 3.7 %, because one building has several entrances and two portals geocode one
    # advert to two of them — while both catch 2 of 64 hazard-class pairs, which is nothing.
    # The obec never differs on a known duplicate (0 of 1,317) and the street differs on 1.7 %.
    # E135/M199: the town separates two adverts only when BOTH sides are resolved at street
    # grain or finer. Over 140 obce, 533 of 22,421 structurally certain duplicates are recorded
    # under two towns — a village against the district town it is advertised under — and EVERY
    # one of them has a side known only to the obec or the quarter. Conditioned on both sides
    # at street grain the cost is 0 of 2,905. The trial cohort cannot see this: it is SELECTED
    # by obec, so its 0-of-1,317 is a selection effect, not a measurement.
    obec_reads = not cfg.d43_obec_street_grain_only or (
        _at_street_grain(a) and _at_street_grain(b)
    )
    if (obec_reads and a.location.obec_kod is not None and b.location.obec_kod is not None
            and a.location.obec_kod != b.location.obec_kod):
        add("obec", a.location.obec_name or a.location.obec_kod,
            b.location.obec_name or b.location.obec_kod)
    if (a.location.street_key and b.location.street_key
            and a.location.street_key != b.location.street_key
            and not (lenient and _pins_together(a, b, cfg.d43_street_min_distance_m))):
        add("street", a.location.street_key, b.location.street_key)

    # The compass direction, which is what separates two otherwise identical flats in one
    # Petřiny building (28145 x 528717: `na jihovýchod` against `na východ`). Both sides must
    # name exactly ONE direction: a body that lists two is describing a through-flat.
    dirs_a, dirs_b = orientations(a.description), orientations(b.description)
    if len(dirs_a) == 1 and len(dirs_b) == 1 and dirs_a != dirs_b:
        add("orientation", next(iter(dirs_a)), next(iter(dirs_b)))

    units_a, units_b = unit_designators(a.description), unit_designators(b.description)
    if (len(units_a) == 1 and len(units_b) == 1 and units_a != units_b
            and address_block_key(a) == address_block_key(b)):
        add("unit_designator", next(iter(units_a)), next(iter(units_b)))

    # E140/E141/E142/E143: the four W15 readings. Every one of them is a fact the advert
    # PRINTS (or, for the signature, two numbers the portal prints for it), so all three modes
    # read them — E138's "inferred rather than stated" exemption does not reach any of them.
    if cfg.d43_parcel_numbers:
        parcels_a = selected_parcels(a, cfg)
        parcels_b = selected_parcels(b, cfg)
        if _set_conflict(parcels_a, parcels_b):
            add("parcel", sorted(parcels_a), sorted(parcels_b))

    if cfg.d43_accessory_designators:
        acc_a = accessory_designators(a.description)
        acc_b = accessory_designators(b.description)
        for kind in sorted(set(acc_a) & set(acc_b)):
            if _set_conflict(acc_a[kind], acc_b[kind]):
                add("accessory", f"{kind}={sorted(acc_a[kind])}",
                    f"{kind}={sorted(acc_b[kind])}")
                break

    if cfg.d43_offered_extent:
        extent = offered_extent(a, b, cfg)
        if extent is not None:
            add("extent", extent[0], extent[1])

    if cfg.d43_two_unit_signature and two_unit_signature(a, b, cfg):
        add("two_unit", f"{headline_area(a)} m2 / {a.price}",
            f"{headline_area(b)} m2 / {b.price}")

    # E153: the area the BODY prints, read WITHOUT the stored column and compared by the
    # ROUNDING rule. This is the only reader that can see into the 16 % of the corpus whose
    # stored headline is a terrace, a cellar or the plot.
    if cfg.d43_printed_area:
        printed = printed_area_conflict_cfg(a, b, cfg)
        if printed is not None:
            add("printed_area", printed[0], printed[1])

    # E186: the size the two bodies LEAD with. Read only where the set reader has already
    # abstained — one HK-Zámeček advert offers `stavební pozemek o výměře 732 m²` and names the
    # 2 195 m² parcel it will be cut from, the other offers the 2 195 m² parcel itself, and as
    # sets they share 2 195 and meet. Both sides must lead with a figure, and the two must be
    # apart by more than the rounding of the coarser.
    if cfg.d43_offer_area:
        offer = _offer_area_conflict(a, b, cfg, is_land)
        if offer is not None:
            add("offer_area", offer[0], offer[1])

    # E161: the unit code the body PRINTS, whole and in the bare form. `unit_designator` reads
    # a keyword and truncates at the word boundary, so `B2.2.1` against `B1.2.1` read B2 against
    # B1 under a keyword and nothing at all without one. Two segments minimum — a bare `B2` is
    # a building and every flat in it shares it — and an empty set is never a conflict.
    # E166: the storey the BODY prints, read under the SAME rule as the column — a gap of two
    # is a fact anywhere, a gap of one only inside one feed, because `přízemí` is written both
    # ways and 45 % of cross-portal known duplicates carry a one-storey gap for that reason. It
    # is read because the stored column is null on one side of a third of the corpus, and
    # because `body_align` must not read a storey (E163: doing so newly split 126 certain
    # duplicates of two cohorts on nothing else).
    if cfg.d43_prose_floor:
        floors_a, floors_b = printed_floors(a.description), printed_floors(b.description)
        if floors_a and floors_b and not (floors_a & floors_b):
            prose_gap = min(abs(x - y) for x in floors_a for y in floors_b)
            feed = _same_feed(a, b, cfg.floor_same_source_feed, cfg.floor_feed_unknown_closed)
            if (_rounded_floors(a, b, cfg, prose_gap)
                    or (prose_gap == 1 and feed)):
                add("prose_floor", sorted(floors_a), sorted(floors_b))

    # E181: the storey stated OF THE OFFERED UNIT. `prose_floor` is a set of every storey the
    # body names, and one Dašice mill advert names its own 2.NP and a WC in 1.NP, so the sets
    # meet and the two floors of one mill never contradict. The placement clause names one.
    if cfg.d43_subject_floor:
        subject_a, subject_b = subject_floors(a.description), subject_floors(b.description)
        if subject_a and subject_b and not (subject_a & subject_b):
            subject_gap = min(abs(x - y) for x in subject_a for y in subject_b)
            feed = _same_feed(a, b, cfg.floor_same_source_feed, cfg.floor_feed_unknown_closed)
            if _rounded_floors(a, b, cfg, subject_gap) or (subject_gap == 1 and feed):
                add("subject_floor", sorted(subject_a), sorted(subject_b))

    # E181: the storey written in WORDS. `v přízemí` against `v patře` carries no digit, so no
    # numbered reader sees it — and it needs no camp table either, because `přízemí` is the
    # ground floor on every portal. One HK-Pouchov 3+kk is `s terasou 15 m2 v přízemí` on
    # realitymix and `s balkonem v patře` on four other portals at the same 17,000 rent.
    if cfg.d43_ground_vs_upper:
        words_a, words_b = ground_or_upper(a.description), ground_or_upper(b.description)
        if len(words_a) == 1 and len(words_b) == 1 and words_a != words_b:
            add("storey_word", next(iter(words_a)), next(iter(words_b)))

    # E183: how many dwellings the object holds. D49 refused the bare co-live price limb, and
    # that refusal stands — one advert may carry a freehold price and a co-operative share at
    # the same moment. A stated COUNT is not that case: `dům se 2 byty` at 11,100,000 against
    # `dům se 4 byty` at 21,500,000, both live on bazos for 7.3 days, are two houses.
    if cfg.d43_stated_unit_count != "off" and not is_land:
        counts_a = stated_unit_counts(a.description)
        counts_b = stated_unit_counts(b.description)
        if len(counts_a) == 1 and len(counts_b) == 1 and counts_a != counts_b:
            conjunction = cfg.d43_stated_unit_count == "always" or (
                _co_live(a, b, cfg.d43_price_colive_min_overlap_days)
                and not price_paths_agree(a, b, cfg.d43_price_path_tol))
            if conjunction:
                add("unit_count", next(iter(counts_a)), next(iter(counts_b)))

    if cfg.d43_unit_codes:
        codes_a = printed_unit_codes(a.description, cfg.d43_unit_codes_wide)
        codes_b = printed_unit_codes(b.description, cfg.d43_unit_codes_wide)
        if _set_conflict(codes_a, codes_b):
            add("unit_code", sorted(codes_a), sorted(codes_b))

    # E151/E152: the street and the town the BODY names, for the adverts whose resolved
    # location cannot separate them — two Olomouc office blocks with no street key, a Droždín
    # plot against one in Oplocany u Tovačova.
    if cfg.d43_prose_street:
        streets = prose_street_conflict(a, b)
        if streets is not None:
            add("street_prose", streets[0], streets[1])
    if cfg.d43_prose_obec and prose_obec_conflict(a, b):
        add("obec_prose", a.location.obec_name or "", b.location.obec_name or "")

    # E150: the reader that knows no form. Last, because it is the most expensive — the other
    # readers have already answered for every pair whose form somebody wrote down.
    if cfg.d43_body_align:
        aligned = aligned_difference(a.description, b.description,
                                     cfg.d43_body_align_min_ratio,
                                     cfg.d43_body_align_heal)
        if aligned is not None:
            add("body_align", aligned[0], aligned[1])

    if mode == GATE and not cfg.d43_gate_image_facts:
        return out
    if mode == CLUSTER and not cfg.d43_cluster_image_facts:
        return out
    floorplan_conflict = _present(feats, "floorplan_conflict")
    room_clip = _present(feats, "tag_room_clip_min2")
    if (floorplan_conflict == 1.0 and room_clip is not None
            and room_clip < FLOORPLAN_ROOM_CLIP_FLOOR):
        add("floorplan", "conflict", f"room_clip_min2={room_clip:.3f}")
    if room_clip is not None and room_clip < ROOM_CLIP_FLOOR:
        add("interior", f"room_clip_min2={room_clip:.3f}", f"floor={ROOM_CLIP_FLOOR}")

    return out


def indistinguishable(
    a: Listing,
    b: Listing,
    feats: Feats | None = None,
    settings: Settings | None = None,
    mode: str = PROMOTE,
) -> bool:
    """D43's predicate: no stated fact tells these two units apart."""
    return not distinguishing_facts(a, b, feats, settings, mode)


# The nine comparable attributes an operator can check by reading the two adverts. Absence of a
# fact is cheap when an advert states almost nothing, so this is the direct measure of what the
# model score was proxying for — and unlike a score it is a sentence: "they state and agree on
# street and area, and neither contradicts the other".
AGREEING_ATTRIBUTES: tuple[str, ...] = (
    "area",
    "disposition",
    "floor",
    "total_floors",
    "price",
    "street",
    "stated_area",
    "orientation",
    "room_photo",
)


def agreeing_attributes(
    a: Listing,
    b: Listing,
    feats: Feats | None = None,
    settings: Settings | None = None,
) -> list[str]:
    """Which of `AGREEING_ATTRIBUTES` both adverts STATE and agree on."""
    cfg = settings or Settings()
    out: list[str] = []
    if a.area_m2 and b.area_m2 and area_relation(a.area_m2, b.area_m2, cfg) == "support":
        out.append("area")
    if a.disposition and b.disposition and a.disposition == b.disposition:
        out.append("disposition")
    # Both floors STATED is the agreement here: whether they read equal is the convention's
    # business (E133), and the predicate has already refused the pair if the residual is a fact.
    if a.floor is not None and b.floor is not None:
        out.append("floor")
    if (a.total_floors is not None and b.total_floors is not None
            and (a.total_floors == b.total_floors
                 or (cfg.floor_camps_reads != "off"
                     and joint_convention_shift(cfg.floor_camps, a.source, a.floor,
                                                a.total_floors, b.source, b.floor,
                                                b.total_floors)))):
        out.append("total_floors")
    if a.price and b.price:
        out.append("price")
    if a.location.street_key and b.location.street_key and (
        a.location.street_key == b.location.street_key
    ):
        out.append("street")
    areas_a, areas_b = stated_areas(a.description, a.area_m2), stated_areas(b.description,
                                                                           b.area_m2)
    if areas_a and areas_b and (areas_a & areas_b):
        out.append("stated_area")
    dirs_a, dirs_b = orientations(a.description), orientations(b.description)
    if len(dirs_a) == 1 and len(dirs_b) == 1 and dirs_a == dirs_b:
        out.append("orientation")
    room_clip = _present(feats, "tag_room_clip_min2")
    if room_clip is not None and room_clip >= ROOM_CLIP_FLOOR:
        out.append("room_photo")
    return out


def promotion_warrant(
    a: Listing,
    b: Listing,
    feats: Feats | None = None,
    settings: Settings | None = None,
) -> str | None:
    """E131: why this band pair may be promoted, or None when it may not.

    `predicate` is the bare rule — no stated fact, nothing else asked. `agree:<n>` is the
    evidence rail. `photo` is the rail's alternative: one tight non-catalogue frame in common
    says the two galleries photographed one home, which is what counting stated fields stands
    in for — and it does not penalise the portals whose adverts are prose.
    """
    cfg = settings or Settings()
    if distinguishing_facts(a, b, feats, cfg):
        return None
    bar = cfg.d43_promote_min_agreeing
    if bar <= 0:
        return "predicate"
    if len(agreeing_attributes(a, b, feats, cfg)) >= bar:
        return f"agree:{bar}"
    if cfg.d43_promote_photo_alternative and tight_photo_match(feats):
        return "photo"
    return None
