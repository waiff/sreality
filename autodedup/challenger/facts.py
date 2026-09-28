"""The challenger's fact rung (GLOBAL_SEARCH 1.4, rung 1): what two adverts STATE differently.

A fact reads BOTH sides or nothing (E12). Seven typed facts compare two stated values under the
`Dials`: deal and kind (rule 15; dům <-> komerční is compatible), area (or the plot of a house or a
parcel), disposition, floor, price (two price paths that never name one amount within `price` while
co-live, or within `colive_price` while co-live on one portal; paths `price_units` apart are two
units of measure and say nothing), and the unit: E61 itself (`guards.unit_designator_conflict`) or a
token the two bodies align on and differ in. The street limb is dropped (D5): street keys are score
evidence. Two adverts co-live on ONE portal state their columns in one convention, so there a
smaller gap already is a stated one (`colive_*`).

Then the fourteen case readers the ladder owns, read by `distinguishing_facts` itself and kept by
name (six body-stated readers and the K10 case readers; `obec_prose` is both). A fact reads the two
adverts and nothing else: no feature row reaches a reader, so no photograph excuses a stated fact
(K-P; E305r's tight frames are never read) and tags are facts in no mode. A pair reads the same
whether anybody scored it or not."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Callable, Mapping

from autodedup.body_align import aligned_difference
from autodedup.dataset import Listing
from autodedup.features import plot_area, rel_diff
from autodedup.guards import unit_designator_conflict
from autodedup.indistinguishable import (
    CLUSTER,
    _price_points,
    distinguishing_facts,
    effective_area,
    honest_overlap_days,
    price_paths_agree,
)
from autodedup.settings import Settings
from toolkit.room_taxonomy import category_main_compatible

RULE_15: frozenset[str] = frozenset({"deal", "kind"})
TYPED: tuple[str, ...] = ("deal", "kind", "area", "disposition", "floor", "price", "unit",
                          "body_align")
READERS: tuple[str, ...] = (
    "printed_area", "prose_floor", "storey_word", "obec_prose", "street_prose", "cellar_area",
    "outdoor_accessory", "accessory_price", "parcel", "position_designator", "lot_label",
    "printed_designator", "unit_code", "labelled_unit")
LAND: str = "pozemek"
FLAT: str = "byt"
PLOT_OBJECTS: frozenset[str] = frozenset({"dum", LAND})


@dataclass(frozen=True)
class Dials:
    """Ten of the challenger's thirteen dials (the score's two cuts and the union's `t_neg` are
    the other three)."""

    area: float = 0.08
    plot: float = 0.05
    floor: int = 2
    price: float = 0.05
    colive_days: float = 1.0
    price_units: float = 10.0
    colive_floor: int = 1
    colive_price: float = 0.005
    colive_area: float = 0.005
    body_align: float = 0.6


Fact = Callable[[int, int], "str | None"]


def stated_difference(listings: Mapping[int, Listing], settings: Settings,
                      dials: Dials = Dials()) -> Fact:
    """The one fact function over `listings`, at pair and group grain alike: `fact(a, b)` names
    the first fact the two adverts state differently (TYPED, then READERS order), or None. The
    settings row governs the readers only; the typed facts read the dials."""
    readers_row = dataclasses.replace(settings, d43_body_align=False, d43_body_align_heal=False)

    def fact(x: int, y: int) -> str | None:
        a, b = listings[x], listings[y]
        typed = _typed(a, b, settings, dials)
        if typed is not None:
            return typed
        if unit_designator_conflict(a, b) is not None:
            return "unit"
        if aligned_difference(a.description, b.description, dials.body_align, True) is not None:
            return "body_align"
        return _reader(a, b, readers_row)

    return fact


def _typed(a: Listing, b: Listing, settings: Settings, d: Dials) -> str | None:
    if a.category_type and b.category_type and a.category_type != b.category_type:
        return "deal"
    if not category_main_compatible(a.category_main, b.category_main):
        return "kind"
    together = honest_overlap_days(a, b)
    colive = together is not None and together >= d.colive_days
    one_portal = colive and a.source is not None and a.source == b.source
    area_a, area_b = effective_area(a, settings), effective_area(b, settings)
    area_tol = d.colive_area if one_portal else d.area
    if area_a and area_b and area_a > 0 and area_b > 0 and rel_diff(area_a, area_b) > area_tol:
        return "area"
    if {a.category_main, b.category_main} <= PLOT_OBJECTS:
        plot_a, plot_b = plot_area(a), plot_area(b)
        if plot_a and plot_b and plot_a > 0 and plot_b > 0 and rel_diff(plot_a, plot_b) > d.plot:
            return "area"
    if (LAND not in (a.category_main, b.category_main) and a.disposition and b.disposition
            and a.disposition != b.disposition):
        return "disposition"
    if (a.category_main == FLAT and b.category_main == FLAT and a.floor is not None
            and b.floor is not None):
        gap = abs(a.floor - b.floor)
        if gap >= d.floor or (one_portal and gap >= d.colive_floor):
            return "floor"
    left, right = sorted(set(_price_points(a))), sorted(set(_price_points(b)))
    if left and right:
        mid_a, mid_b = left[len(left) // 2], right[len(right) // 2]
        if max(mid_a, mid_b) / min(mid_a, mid_b) <= d.price_units:
            if colive and not price_paths_agree(a, b, d.price):
                return "price"
            if one_portal and not price_paths_agree(a, b, d.colive_price):
                return "price"
    return None


def _reader(a: Listing, b: Listing, row: Settings) -> str | None:
    """The first of READERS that `distinguishing_facts` fires on the two adverts alone."""
    fired = {f.name for f in distinguishing_facts(a, b, None, row, CLUSTER)}
    return next((name for name in READERS if name in fired), None)
