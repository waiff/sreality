"""A per-m² figure must never masquerade as `listings.price_czk`.

`price_czk` is a TOTAL (or a monthly rent) on all nine portals — production
carries only `za nemovitost` / `za mesic` / `celkem` / `měsíc`, none per-area. A
unit price stored there reads as a total in every downstream consumer (Kč/m²
stats, estimation comparables, Browse sort, price-drop watchdogs), which is
strictly worse than the missing value it replaces.

The masquerade is ONGOING, not historical: ceskereality, realitymix and bazos
carry ~310 m² commercial units whose stored price_czk has a median of 136 / 176 /
379 Kč, with rows first seen the day this was written. Two rails, both here:

  1. the six text-price portals refuse the cell at parse time
     (`scraper.price_text.is_per_area_price`);
  2. the three JSON portals take a numeric price with no unit text to inspect,
     so their rail is the write boundary (`scraper.db.plausible_price_czk`),
     which also backs up rail 1 for a page shape no parser has seen yet.
"""

from __future__ import annotations

import pytest

from scraper import db
from scraper.bazos_parser import _parse_price as bazos_price
from scraper.ceskereality_parser import _parse_price as ceskereality_price
from scraper.idnes_parser import _parse_price as idnes_price
from scraper.maxima_parser import _parse_price as maxima_price
from scraper.realitymix_parser import _parse_price as realitymix_price
from scraper.remax_parser import _detail_price as remax_price

# Every portal whose price arrives as TEXT. One shared parse rail, so the same
# corpus must hold for all six (rule 21 — no per-portal branches).
TEXT_PRICE_PARSERS = [
    pytest.param(bazos_price, id="bazos"),
    pytest.param(ceskereality_price, id="ceskereality"),
    pytest.param(idnes_price, id="idnes"),
    pytest.param(maxima_price, id="maxima"),
    pytest.param(realitymix_price, id="realitymix"),
    pytest.param(remax_price, id="remax"),
]

# Seeded from test_idnes_parser's six positives (incl. the spaced "18 500 Kč / m²")
# and test_remax_parser's "7 759 CZK/ za m2", which the naive digit scrape used to
# turn into 77592 by swallowing the "2" out of "m2".
PER_AREA_CELLS = [
    "18 500 Kč/m²",
    "18 500 Kč / m²",
    "2 500 Kč za m²",
    "1 200 Kč/m²/rok",
    "150 Kč/m2/měsíc",
    "7\xa0759\n\t\tCZK/ za m2",
]

# The two negatives are the point of the anchoring: a monthly rent is not a unit
# price, and a total with a per-m² NOTE beside it is still a total.
TOTAL_CELLS = [
    ("14 160 Kč/měsíc", 14_160),
    ("4 990 000 Kč (4 008 Kč/m² )", 4_990_000),
    ("3 190 000 Kč", 3_190_000),
]


@pytest.mark.parametrize("parse", TEXT_PRICE_PARSERS)
@pytest.mark.parametrize("cell", PER_AREA_CELLS)
def test_per_area_cell_yields_null(parse, cell: str) -> None:
    assert parse(cell, "prodej")[0] is None


@pytest.mark.parametrize("parse", TEXT_PRICE_PARSERS)
@pytest.mark.parametrize("cell,expected", TOTAL_CELLS)
def test_total_cell_survives(parse, cell: str, expected: int) -> None:
    # remax reads the amount only from the part before "CZK"; its cell never
    # carries a bare "Kč", so give each parser its own currency spelling.
    if parse is remax_price:
        cell = cell.replace("Kč", "CZK", 1)
    assert parse(cell, "prodej")[0] == expected


@pytest.mark.parametrize("cell", PER_AREA_CELLS)
def test_the_unit_is_never_mistaken_for_an_agenda(cell: str) -> None:
    # price_unit stays the agenda's own label; it is a duplicate of category_type
    # and must never be pressed into service as an area unit (migration 423).
    assert idnes_price(cell, "pronajem")[1] == "za mesic"
    assert idnes_price(cell, "prodej")[1] == "za nemovitost"


# ---- rail 2: the JSON portals, and the backstop for everyone -----------------

# (source, the per-m2 price a contaminated row actually stored, category_main)
JSON_PRICE_PORTALS = [
    pytest.param("sreality", 136, "komercni", id="sreality"),
    pytest.param("bezrealitky", 176, "komercni", id="bezrealitky"),
    pytest.param("mmreality", 379, "komercni", id="mmreality"),
]


@pytest.mark.parametrize("source,unit_price,category_main", JSON_PRICE_PORTALS)
def test_write_boundary_floors_a_unit_price(
    source: str, unit_price: int, category_main: str
) -> None:
    assert db.plausible_price_czk(
        unit_price, category_type="prodej", category_main=category_main
    ) is None
    assert db.plausible_price_czk(
        unit_price, category_type="pronajem", category_main=category_main
    ) is None


def test_write_boundary_keeps_real_prices() -> None:
    assert db.plausible_price_czk(
        3_190_000, category_type="prodej", category_main="byt"
    ) == 3_190_000
    assert db.plausible_price_czk(
        14_160, category_type="pronajem", category_main="byt"
    ) == 14_160


def test_land_has_no_sale_floor() -> None:
    # A small parcel really does sell for tens of thousands; a floor there would
    # delete real prices to catch a masquerade land pages don't have.
    assert db.plausible_price_czk(
        45_000, category_type="prodej", category_main="pozemek"
    ) == 45_000


def test_no_category_means_no_floor() -> None:
    # The queue's index-price path has no category to reason about; it must stay
    # pure column-range clamping there.
    assert db.plausible_price_czk(136) == 136
    assert db.plausible_price_czk(None) is None


def test_area_floor_drops_parse_artifacts() -> None:
    assert db.plausible_area_m2(1.0) is None
    assert db.plausible_area_m2(4.9) is None
    assert db.plausible_area_m2(5.0) == 5.0
    assert db.plausible_area_m2(310.0) == 310.0
    assert db.plausible_area_m2(None) is None


def test_plausibility_runs_on_a_whole_row() -> None:
    row = {
        "price_czk": 136,
        "area_m2": 310.0,
        "category_type": "prodej",
        "category_main": "komercni",
    }
    db.plausible_listing_row(row)
    assert row["price_czk"] is None
    assert row["area_m2"] == 310.0
