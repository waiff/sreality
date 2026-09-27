"""The hard constraints of the model-first engine: SEVEN typed fact comparators, nothing else.

The operator's definition of a false merge is a merge across a STATED distinguishing fact. So the
only rules that may forbid a merge are comparisons of two stated values of one typed fact; every
other signal (text, photos, broker, place, time) is evidence and belongs to the learned score.

A fact reads BOTH sides or nothing (E12: missing is never a mismatch). The comparators read a
compact per-advert record (`Rec`), so the group check can compare any two members, scored or not.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from autodedup.dataset import Listing, live_end_stamp
from autodedup.features import plot_area
from autodedup.indistinguishable import effective_area
from autodedup.settings import Settings
from autodedup.text_facts import address_block_key, unit_designators
from toolkit.room_taxonomy import category_main_compatible

FACTS: tuple[str, ...] = ("deal", "kind", "area", "disposition", "floor", "price", "place")
LAND = "pozemek"
FLAT = "byt"
PLOT_OBJECTS = frozenset({"dum", LAND})


@dataclass(frozen=True, slots=True)
class FactCfg:
    area_tol: float = 0.08          # the E5 wall's own bar
    plot_tol: float = 0.05          # house/land parcel, both stated
    floor_delta: int = 2            # flats, both stated (the floor wall)
    price_tol: float = 0.05         # paths never name one amount within this ...
    price_colive_days: float = 1.0  # ... while both adverts were live together this long
    price_units: float = 10.0       # paths further apart than this ratio are two UNITS (per m2,
                                    # per year, total), not two amounts: incomparable, no fact
    street: bool = True             # both at street grain and the streets differ
    unit: bool = True               # E61's predicate: one designator each, differ, one address

    def dials(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


@dataclass(frozen=True, slots=True)
class Rec:
    id: int
    source: str | None
    block: str
    ctype: str | None
    cmain: str | None
    area: float | None
    plot: float | None
    dispo: str | None
    floor: int | None
    prices: tuple[float, ...]
    start: float | None
    end: float | None
    street: str | None
    addr: str
    units: frozenset


def _epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def rec_of(listing: Listing, settings: Settings) -> Rec:
    points = [float(p) for _, p in (listing.price_history or ()) if p is not None and float(p) > 0]
    if listing.price and float(listing.price) > 0:
        points.append(float(listing.price))
    return Rec(
        id=listing.id, source=listing.source, block=listing.block,
        ctype=listing.category_type, cmain=listing.category_main,
        area=effective_area(listing, settings), plot=plot_area(listing),
        dispo=listing.disposition, floor=listing.floor, prices=tuple(sorted(set(points))),
        start=_epoch(listing.first_seen_at), end=_epoch(live_end_stamp(listing)),
        street=listing.location.street_key, addr=address_block_key(listing),
        units=frozenset(unit_designators(listing.description)),
    )


def _rel(a: float, b: float) -> float:
    return abs(a - b) / max(a, b)


def colive_days(a: Rec, b: Rec) -> float | None:
    if None in (a.start, a.end, b.start, b.end):
        return None
    return max(0.0, (min(a.end, b.end) - max(a.start, b.start)) / 86400.0)


def _paths_meet(a: Rec, b: Rec, tol: float) -> bool:
    return any(_rel(x, y) <= tol for x in a.prices for y in b.prices)


def _comparable(left: tuple[float, ...], right: tuple[float, ...], ratio: float) -> bool:
    x, y = left[len(left) // 2], right[len(right) // 2]
    return max(x, y) / min(x, y) <= ratio


def facts(a: Rec, b: Rec, cfg: FactCfg = FactCfg(), first: bool = False) -> list[str]:
    """Every one of the seven facts the two adverts state differently (in FACTS order)."""
    out: list[str] = []

    def hit(name: str) -> bool:
        out.append(name)
        return first

    if a.ctype and b.ctype and a.ctype != b.ctype and hit("deal"):
        return out
    if not category_main_compatible(a.cmain, b.cmain) and hit("kind"):
        return out
    area_apart = (a.area and b.area and a.area > 0 and b.area > 0
                  and _rel(a.area, b.area) > cfg.area_tol)
    plot_apart = ({a.cmain, b.cmain} <= PLOT_OBJECTS and a.plot and b.plot and a.plot > 0
                  and b.plot > 0 and _rel(a.plot, b.plot) > cfg.plot_tol)
    if (area_apart or plot_apart) and hit("area"):
        return out
    if (LAND not in (a.cmain, b.cmain) and a.dispo and b.dispo and a.dispo != b.dispo
            and hit("disposition")):
        return out
    if (a.cmain == FLAT and b.cmain == FLAT and a.floor is not None and b.floor is not None
            and abs(a.floor - b.floor) >= cfg.floor_delta and hit("floor")):
        return out
    if (a.prices and b.prices and not _paths_meet(a, b, cfg.price_tol)
            and _comparable(a.prices, b.prices, cfg.price_units)):
        together = colive_days(a, b)
        if together is not None and together >= cfg.price_colive_days and hit("price"):
            return out
    street_apart = cfg.street and a.street and b.street and a.street != b.street
    unit_apart = (cfg.unit and len(a.units) == 1 and len(b.units) == 1 and a.units != b.units
                  and a.addr == b.addr)
    if (street_apart or unit_apart) and hit("place"):
        return out
    return out


def first_fact(a: Rec, b: Rec, cfg: FactCfg = FactCfg()) -> str | None:
    found = facts(a, b, cfg, first=True)
    return found[0] if found else None


def fact_matrix(recs: dict[int, Rec], pairs: Iterable[tuple[int, int]],
                cfg: FactCfg = FactCfg()) -> list[list[str]]:
    return [facts(recs[a], recs[b], cfg) for a, b in pairs]
