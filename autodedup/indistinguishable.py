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
from autodedup.features import plot_area, rel_diff
from autodedup.guards import LAND_CATEGORY, area_relation
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


def distinguishing_facts(
    a: Listing,
    b: Listing,
    feats: Feats | None = None,
    settings: Settings | None = None,
) -> list[Fact]:
    """Every stated fact that differs between two adverts. Empty list = indistinguishable.

    `feats` is one row of the engine's own feature vector, and is the only way the image facts
    can be read — this module computes no image evidence of its own. Passing None simply drops
    those two facts, which is the correct reading of an advert whose photographs nobody paired.
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

    if area_relation(a.area_m2, b.area_m2, cfg) not in ("support", "unknown"):
        add("area", a.area_m2, b.area_m2)

    areas_a = stated_areas(a.description, a.area_m2)
    areas_b = stated_areas(b.description, b.area_m2)
    if areas_disjoint(areas_a, areas_b):
        add("stated_area", sorted(areas_a), sorted(areas_b))

    is_land = LAND_CATEGORY in (a.category_main, b.category_main)
    if (not is_land and a.disposition is not None and b.disposition is not None
            and a.disposition != b.disposition):
        add("disposition", a.disposition, b.disposition)

    if a.floor is not None and b.floor is not None:
        delta = abs(a.floor - b.floor)
        # A one-floor gap means two different things on the two sides of a portal boundary, so
        # it counts only within one portal — `features.floor_stated_conflict`, same rule.
        if delta >= 2 or (delta == 1 and a.source is not None and a.source == b.source):
            add("floor", a.floor, b.floor)

    if (a.total_floors is not None and b.total_floors is not None
            and a.total_floors != b.total_floors):
        add("total_floors", a.total_floors, b.total_floors)

    plot_a, plot_b = plot_area(a), plot_area(b)
    if plot_a and plot_b and rel_diff(plot_a, plot_b) > PLOT_TOL:
        add("plot_area", plot_a, plot_b)

    if a.price and b.price and a.price > 0 and b.price > 0:
        gap = rel_diff(float(a.price), float(b.price))
        cross = a.source is not None and b.source is not None and a.source != b.source
        if gap > (PRICE_CROSS_TOL if cross else PRICE_SAME_SOURCE_TOL):
            add("price", a.price, b.price)

    # WHERE the advert says the unit is. Only the two coarse keys are read. The finer ones were
    # measured and refused: a RÚIAN address code differs on 4.2 % of known duplicates and a house
    # number on 3.7 %, because one building has several entrances and two portals geocode one
    # advert to two of them — while both catch 2 of 64 hazard-class pairs, which is nothing.
    # The obec never differs on a known duplicate (0 of 1,317) and the street differs on 1.7 %.
    if (a.location.obec_kod is not None and b.location.obec_kod is not None
            and a.location.obec_kod != b.location.obec_kod):
        add("obec", a.location.obec_name or a.location.obec_kod,
            b.location.obec_name or b.location.obec_kod)
    if (a.location.street_key and b.location.street_key
            and a.location.street_key != b.location.street_key):
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
) -> bool:
    """D43's predicate: no stated fact tells these two units apart."""
    return not distinguishing_facts(a, b, feats, settings)
