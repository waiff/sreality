"""The lab's CI fixture cohort: the engine e2e cohort (synthetic adverts, PROGRAM.md 2's six shapes)
plus three synthetic pairs that reach the rungs it does not, so `lab verify` in CI reads every rung
of the reference ladder at least once. No real advert is in here."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

from tests.autodedup.test_engine_e2e import (
    BLOCK_A,
    _gallery,
    _listing,
    _location,
    _spread_hashes,
    build_records,
)

NUMERAL_A, NUMERAL_B = 1601, 1602
UNIT_A, UNIT_B = 1701, 1702
GATE_A, GATE_B = 1801, 1802

TEXT_FLOOR = ("Nabizime k prodeji svetly byt 2+kk v cihlovem dome na ulici Nabrezni v Turnove. "
              "Byt se nachazi ve {floor}. patre domu s vytahem, ma sklep a zasklenou lodzii do "
              "vnitrobloku. Dum prosel rekonstrukci strechy, v dosahu je skola i zastavka.")
TEXT_UNIT = ("Prodej bytove jednotky c. {unit} v novem bytovem dome na ulici Luzicka v Turnove. "
             "Jednotka ma dispozici 3+kk, balkon, sklepni kotec a parkovaci stani v garazi. "
             "Energeticky usporny dum s rekuperaci, dokonceni v minulem roce.")


def records() -> list[dict[str, Any]]:
    rows = build_records()
    # A body-stated floor conflict on an otherwise shared advert: the numerals auto-reject.
    numeral = {"street_key": "turnov|nabrezni", "house_number": "77", "house_number_cp": "77",
               "granularity": "address", "granularity_rank": 80, "is_address_grain": True}
    for listing_id, floor, source in ((NUMERAL_A, 3, "sreality"), (NUMERAL_B, 5, "idnes")):
        rows.append(_listing(listing_id, BLOCK_A, source=source, disposition="2+kk",
                             area_m2=54.0, floor=None, price=3_950_000,
                             description=TEXT_FLOOR.format(floor=floor),
                             location=_location(**numeral)))
        rows.extend(_gallery(listing_id, 510_000 + 10 * listing_id,
                             _spread_hashes(listing_id, 3)))
    # Two units of one house, each body naming its own unit number: E61's designator veto.
    unit = {"street_key": "turnov|luzicka", "house_number": "905", "house_number_cp": "905",
            "granularity": "address", "granularity_rank": 80, "is_address_grain": True}
    shared = _spread_hashes(UNIT_A, 4)
    for listing_id, number in ((UNIT_A, 12), (UNIT_B, 14)):
        rows.append(_listing(listing_id, BLOCK_A, disposition="3+kk", area_m2=81.0, floor=2,
                             price=7_800_000, description=TEXT_UNIT.format(unit=number),
                             location=_location(**unit)))
        rows.extend(_gallery(listing_id, 520_000 + 10 * listing_id, shared))
    # One shoot in gallery order (K-C) on one portal, on two flats a floor apart: the D43 gate.
    shoot = _spread_hashes(GATE_A, 5)
    for listing_id, floor, source in ((GATE_A, 2, "sreality"), (GATE_B, 3, "sreality")):
        rows.append(_listing(listing_id, BLOCK_A, source=source, disposition="2+1",
                             area_m2=66.0, floor=floor, total_floors=5, price=4_300_000,
                             description=None,
                             location=_location(street_key="turnov|jiraskova")))
        rows.extend(_gallery(listing_id, 530_000 + 10 * listing_id, shoot))
    return rows


def write(path: Path) -> Path:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for record in records():
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path

