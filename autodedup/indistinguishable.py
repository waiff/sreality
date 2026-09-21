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
from typing import Mapping, Sequence

from autodedup.dataset import Listing
from autodedup.features import STREET_GRAIN_RANK, haversine_m, plot_area, rel_diff
from autodedup.floor_convention import (
    convention_ambiguous,
    convention_known,
    floor_gap,
    joint_convention_shift,
)
from autodedup.guards import LAND_CATEGORY, area_rel_diff, area_relation
from autodedup.settings import Settings
from autodedup.structural_truth import areas_disjoint
from autodedup.text_facts import (
    address_block_key,
    orientations,
    stated_areas,
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
    "floorplan",
    "interior",
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
        same_source = a.source is not None and a.source == b.source
        strict = reads == "strict" and convention_known(cfg.floor_camps, a.source, b.source)
        if (gap != 0) if strict else (abs(gap) >= 2 or (abs(gap) == 1 and same_source)):
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

    plot_a, plot_b = plot_area(a), plot_area(b)
    if plot_a and plot_b and rel_diff(plot_a, plot_b) > PLOT_TOL:
        add("plot_area", plot_a, plot_b)

    if a.price and b.price and a.price > 0 and b.price > 0:
        price_gap = rel_diff(float(a.price), float(b.price))
        cross = a.source is not None and b.source is not None and a.source != b.source
        over = price_gap > (PRICE_CROSS_TOL if cross else PRICE_SAME_SOURCE_TOL)
        if cfg.d43_price_path:
            agree = price_paths_agree(a, b, cfg.d43_price_path_tol)
            # E134: the momentary gap is excused by an agreeing path; a CONTRADICTION — two
            # adverts on sale at the same time that never named one another's price — is a
            # fact at the cross-portal bar whether or not they share a portal.
            contradiction = (cfg.d43_price_colive_contradiction and not agree
                             and price_gap > PRICE_CROSS_TOL and _windows_overlap(a, b))
            if (over and not agree) or contradiction:
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
