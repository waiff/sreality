"""Tests for parser.py against a real-shape Sreality v1 detail fixture.

The live /api/v1/estates detail payload is flat snake_case (id key
`hash_id`, typed attributes at the top level, `{name, value}` enum
objects). The fixture is a real anonymized byt/prodej listing.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from scraper.parser import parse_images, parse_listing

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample() -> dict[str, Any]:
    return json.loads((FIXTURES / "sample_listing.json").read_text("utf-8"))


def _estate(**overrides: Any) -> dict[str, Any]:
    """Minimal valid estate object; override individual fields per test."""
    base: dict[str, Any] = {"hash_id": 1, "locality": {}}
    base.update(overrides)
    return base


def test_sreality_id(sample):
    assert parse_listing(sample)["sreality_id"] == 3292504140


def test_category(sample):
    row = parse_listing(sample)
    assert row["category_main"] == "byt"
    assert row["category_type"] == "prodej"


def test_price_hidden_is_none(sample):
    # This fixture is a "price on request" listing (price_czk == 0).
    row = parse_listing(sample)
    assert row["price_czk"] is None
    assert row["price_unit"] == "celkem"


def test_price_present():
    row = parse_listing(_estate(price_summary_czk=8690000,
                                price_summary_unit_cb={"name": "za nemovitost", "value": 1}))
    assert row["price_czk"] == 8690000
    assert row["price_unit"] == "celkem"


def test_price_unit_monthly():
    row = parse_listing(_estate(price_czk=22500,
                                price_unit_cb={"name": "za měsíc", "value": 4}))
    assert row["price_czk"] == 22500
    assert row["price_unit"] == "měsíc"


def test_area(sample):
    assert parse_listing(sample)["area_m2"] == 85.0


def test_disposition(sample):
    assert parse_listing(sample)["disposition"] == "3+kk"


def test_disposition_falls_back_to_name():
    row = parse_listing(_estate(category_sub_cb={"name": "atypický", "value": 12},
                                advert_name="Prodej bytu 3+1 100 m²"))
    assert row["disposition"] == "3+1"


def test_geo(sample):
    row = parse_listing(sample)
    assert math.isclose(row["lon"], 18.2552, abs_tol=0.001)
    assert math.isclose(row["lat"], 49.8727, abs_tol=0.001)


def test_locality(sample):
    assert parse_listing(sample)["locality"] == "Ostrava - Petřkovice"


def test_district(sample):
    # district_id 65 maps to the canonical okres label.
    assert parse_listing(sample)["district"] == "okres Ostrava-město"


def test_district_okres_label():
    assert parse_listing(_estate(locality={"district_id": 42}))["district"] == "okres Olomouc"


def test_district_falls_back_to_locality_text():
    # Unknown district_id (e.g. -1 for foreign listings) → use the locality's
    # own district text so the country/region name still surfaces.
    row = parse_listing(_estate(locality={"district_id": -1, "district": "Toskánsko"}))
    assert row["district"] == "Toskánsko"


def test_district_collapses_praha_subdistricts():
    assert parse_listing(_estate(locality={"district_id": 5003}))["district"] == "Praha"


def test_locality_ids(sample):
    row = parse_listing(sample)
    assert row["locality_district_id"] == 65
    assert row["locality_region_id"] == 12
    assert row["locality_municipality_id"] == 4730
    assert row["locality_ward_id"] == 8277
    assert row["locality_quarter_id"] == 45


def test_locality_ids_minus_one_sentinel_maps_to_none():
    row = parse_listing(_estate(locality={
        "municipality_id": -1, "quarter_id": -1, "ward_id": -1,
    }))
    assert row["locality_municipality_id"] is None
    assert row["locality_quarter_id"] is None
    assert row["locality_ward_id"] is None


def test_locality_ids_missing_keys_are_none():
    row = parse_listing(_estate(locality={}))
    assert row["locality_municipality_id"] is None
    assert row["locality_quarter_id"] is None
    assert row["locality_ward_id"] is None


def test_floor(sample):
    assert parse_listing(sample)["floor"] == 1


def test_total_floors(sample):
    assert parse_listing(sample)["total_floors"] == 2


def test_total_floors_present():
    row = parse_listing(_estate(floors=6))
    assert row["total_floors"] == 6


def test_amenities(sample):
    row = parse_listing(sample)
    assert row["has_balcony"] is False  # balcony/terrace/loggia all false
    assert row["has_parking"] is False  # parking_lots/garage false, parking null
    assert row["has_lift"] is None      # elevator cb value 0 (unspecified)


def test_lift_yes_no():
    assert parse_listing(_estate(elevator={"name": "Ano", "value": 1}))["has_lift"] is True
    assert parse_listing(_estate(elevator={"name": "Ne", "value": 2}))["has_lift"] is False


def test_building_type(sample):
    assert parse_listing(sample)["building_type"] == "smisena"


def test_panel_building_type():
    row = parse_listing(_estate(building_type={"name": "Panelová", "value": 1}))
    assert row["building_type"] == "panel"


def test_building_type_unspecified_is_none():
    row = parse_listing(_estate(building_type={"name": "- nezadáno", "value": 0}))
    assert row["building_type"] is None


def test_condition(sample):
    assert parse_listing(sample)["condition"] == "velmi dobrý"


def test_condition_unspecified_is_none():
    row = parse_listing(_estate(building_condition={"name": "- vyber stav", "value": 0}))
    assert row["condition"] is None


def test_energy_rating(sample):
    assert parse_listing(sample)["energy_rating"] == "F"


def test_energy_rating_unspecified_is_none():
    row = parse_listing(_estate(energy_efficiency_rating_cb={"name": "Nedefinováno", "value": 0}))
    assert row["energy_rating"] is None


def test_category_fields(sample):
    row = parse_listing(sample)
    assert row["category_sub_cb"] == 6
    assert row["estate_area"] is None      # not a land listing
    assert row["usable_area"] == 85.0
    assert row["garden_area"] == 174.0
    assert row["parking_lots"] is None     # parking field null
    assert row["terrace"] is False
    assert row["cellar"] is False
    assert row["garage"] is False
    assert row["furnished"] == "castecne"  # value 3
    assert row["ownership"] == "osobni"


def test_furnished_known_code():
    row = parse_listing(_estate(furnished={"name": "Vybaveno", "value": 1}))
    assert row["furnished"] == "ano"


def test_furnished_unknown_code_returns_none():
    row = parse_listing(_estate(furnished={"name": "?", "value": 99}))
    assert row["furnished"] is None


def test_ownership_unknown_code_returns_none():
    row = parse_listing(_estate(ownership={"name": "?", "value": 99}))
    assert row["ownership"] is None


def test_amenities_missing_returns_none():
    row = parse_listing(_estate())
    assert row["terrace"] is None
    assert row["cellar"] is None
    assert row["garage"] is None
    assert row["estate_area"] is None
    assert row["parking_lots"] is None
    assert row["has_balcony"] is None
    assert row["has_parking"] is None


def test_parse_images(sample):
    images = parse_images(sample)
    assert len(images) == 14
    assert images[0]["sequence"] == 1
    assert images[0]["url"].startswith("https://d18-a.sdn.cz/")


def test_parse_images_prefixes_scheme():
    raw = _estate(advert_images=[{"url": "//d18-a.sdn.cz/x/y.jpeg", "order": 3}])
    assert parse_images(raw) == [{"url": "https://d18-a.sdn.cz/x/y.jpeg", "sequence": 3}]


def test_description(sample):
    assert parse_listing(sample)["description"].startswith("Prodej samostatné bytové jednotky")


def test_description_strips_whitespace():
    assert parse_listing(_estate(advert_description="  Trimmed.\n\n"))["description"] == "Trimmed."


def test_description_missing_is_none():
    assert parse_listing(_estate())["description"] is None


def test_description_empty_value_is_none():
    assert parse_listing(_estate(advert_description="   "))["description"] is None


def test_missing_id_raises():
    with pytest.raises(ValueError):
        parse_listing({"locality": {}})
