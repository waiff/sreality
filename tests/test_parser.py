"""Tests for parser.py against a curated real-shape Sreality fixture."""

from __future__ import annotations

import copy
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


def test_sreality_id(sample):
    assert parse_listing(sample)["sreality_id"] == 2836292428


def test_category(sample):
    row = parse_listing(sample)
    assert row["category_main"] == "byt"
    assert row["category_type"] == "pronajem"


def test_price(sample):
    row = parse_listing(sample)
    assert row["price_czk"] == 16900
    assert row["price_unit"] == "měsíc"


def test_area(sample):
    assert parse_listing(sample)["area_m2"] == 65.0


def test_disposition(sample):
    assert parse_listing(sample)["disposition"] == "2+kk"


def test_geo(sample):
    row = parse_listing(sample)
    assert math.isclose(row["lon"], 17.2413, abs_tol=0.001)
    assert math.isclose(row["lat"], 49.5692, abs_tol=0.001)


def test_locality(sample):
    assert "Olomouc" in parse_listing(sample)["locality"]


def test_district(sample):
    assert parse_listing(sample)["district"] == "Olomouc - Slavonín"


def test_locality_ids(sample):
    row = parse_listing(sample)
    assert row["locality_district_id"] == 42
    assert row["locality_region_id"] == 8


def test_locality_ids_extended(sample):
    row = parse_listing(sample)
    assert row["locality_municipality_id"] == 500496
    assert row["locality_ward_id"] == 750140
    assert row["locality_quarter_id"] is None  # -1 sentinel maps to None


def test_locality_ids_minus_one_sentinel_maps_to_none():
    raw = {
        "_links": {"self": {"href": "/cs/v2/estates/1"}},
        "recommendations_data": {
            "locality_municipality_id": -1,
            "locality_quarter_id": -1,
            "locality_ward_id": -1,
        },
    }
    row = parse_listing(raw)
    assert row["locality_municipality_id"] is None
    assert row["locality_quarter_id"] is None
    assert row["locality_ward_id"] is None


def test_locality_ids_extended_missing_keys_are_none():
    raw = {
        "_links": {"self": {"href": "/cs/v2/estates/1"}},
        "recommendations_data": {},
    }
    row = parse_listing(raw)
    assert row["locality_municipality_id"] is None
    assert row["locality_quarter_id"] is None
    assert row["locality_ward_id"] is None


def test_floor(sample):
    assert parse_listing(sample)["floor"] == 5


def test_total_floors(sample):
    assert parse_listing(sample)["total_floors"] == 1


def test_total_floors_none_when_no_celkem():
    raw = {
        "_links": {"self": {"href": "/cs/v2/estates/1"}},
        "recommendations_data": {},
        "items": [{"name": "Podlaží", "value": "3. podlaží", "type": "string"}],
    }
    assert parse_listing(raw)["total_floors"] is None


def test_amenities(sample):
    row = parse_listing(sample)
    assert row["has_balcony"] is True
    assert row["has_parking"] is True
    assert row["has_lift"] is True


def test_building_type(sample):
    assert parse_listing(sample)["building_type"] == "cihla"


def test_condition(sample):
    assert parse_listing(sample)["condition"] == "novostavba"


def test_energy_rating(sample):
    assert parse_listing(sample)["energy_rating"] == "B"


def test_category_fields(sample):
    row = parse_listing(sample)
    assert row["category_sub_cb"] == 4
    assert row["estate_area"] == 240.0
    assert row["usable_area"] == 65.0
    assert row["garden_area"] == 60.0
    assert row["parking_lots"] == 1
    assert row["terrace"] is True
    assert row["cellar"] is True
    assert row["garage"] is False
    assert row["furnished"] == "castecne"
    assert row["ownership"] == "osobni"


def test_furnished_unknown_code_returns_none():
    raw = {
        "_links": {"self": {"href": "/cs/v2/estates/1"}},
        "recommendations_data": {"furnished": 99, "category_main_cb": 1},
    }
    assert parse_listing(raw)["furnished"] is None


def test_ownership_unknown_code_returns_none():
    raw = {
        "_links": {"self": {"href": "/cs/v2/estates/1"}},
        "recommendations_data": {"ownership": 99, "category_main_cb": 1},
    }
    assert parse_listing(raw)["ownership"] is None


def test_amenities_missing_returns_none():
    raw = {
        "_links": {"self": {"href": "/cs/v2/estates/1"}},
        "recommendations_data": {"category_main_cb": 1},
    }
    row = parse_listing(raw)
    assert row["terrace"] is None
    assert row["cellar"] is None
    assert row["garage"] is None
    assert row["estate_area"] is None
    assert row["parking_lots"] is None


def test_parse_images(sample):
    images = parse_images(sample)
    assert len(images) == 3
    assert images[0]["sequence"] == 1
    assert images[0]["url"].startswith("https://d18-a.sdn.cz/")


def test_disposition_falls_back_to_meta_description():
    raw = {
        "recommendations_data": {"hash_id": 1},
        "name": {"value": "Generic listing without disposition"},
        "meta_description": "Byt 3+1 100 m² to rent",
    }
    assert parse_listing(raw)["disposition"] == "3+1"


def test_missing_id_raises():
    with pytest.raises(ValueError):
        parse_listing({"items": []})


def test_id_recovered_from_self_href():
    raw = {
        "_links": {"self": {"href": "/cs/v2/estates/12345"}},
        "recommendations_data": {},
    }
    assert parse_listing(raw)["sreality_id"] == 12345


def test_falls_back_to_items_when_recommendations_empty():
    raw = {
        "_links": {"self": {"href": "/cs/v2/estates/999"}},
        "recommendations_data": {},
        "name": {"value": "Pronájem 1+kk"},
        "items": [
            {"name": "Výtah", "value": True, "type": "boolean"},
            {"name": "Parkování", "value": True, "type": "boolean"},
            {"name": "Balkón", "value": True, "type": "boolean"},
        ],
    }
    row = parse_listing(raw)
    assert row["has_lift"] is True
    assert row["has_parking"] is True
    assert row["has_balcony"] is True


def test_panel_building_type():
    raw = {
        "_links": {"self": {"href": "/cs/v2/estates/1"}},
        "recommendations_data": {},
        "items": [{"name": "Stavba", "value": "Panelová", "type": "string"}],
    }
    assert parse_listing(raw)["building_type"] == "panel"


def test_broker_fields(sample):
    row = parse_listing(sample)
    assert row["broker_name"] == "Jana Nováková"
    assert row["broker_email"] == "jana@example.cz"
    assert row["broker_phone"] == "+420777123456"


def test_broker_phone_prefers_mob_over_tel_regardless_of_array_order():
    raw = {
        "_links": {"self": {"href": "/cs/v2/estates/1"}},
        "recommendations_data": {},
        "_embedded": {
            "seller": {
                "user_name": "X",
                "phones": [
                    {"code": "420", "type": "TEL", "number": "111"},
                    {"code": "420", "type": "MOB", "number": "222"},
                ],
            }
        },
    }
    assert parse_listing(raw)["broker_phone"] == "+420222"


def test_broker_phone_empty_country_code_drops_plus_prefix():
    raw = {
        "_links": {"self": {"href": "/cs/v2/estates/1"}},
        "recommendations_data": {},
        "_embedded": {
            "seller": {
                "phones": [{"code": "", "type": "TEL", "number": "222701030"}]
            }
        },
    }
    assert parse_listing(raw)["broker_phone"] == "222701030"


def test_broker_phone_only_tel_falls_back_to_first_tel():
    raw = {
        "_links": {"self": {"href": "/cs/v2/estates/1"}},
        "recommendations_data": {},
        "_embedded": {
            "seller": {
                "phones": [
                    {"code": "420", "type": "TEL", "number": "111"},
                    {"code": "420", "type": "TEL", "number": "222"},
                ]
            }
        },
    }
    assert parse_listing(raw)["broker_phone"] == "+420111"


def test_broker_fields_missing_seller_block_yields_nones():
    raw = {
        "_links": {"self": {"href": "/cs/v2/estates/1"}},
        "recommendations_data": {},
    }
    row = parse_listing(raw)
    assert row["broker_name"] is None
    assert row["broker_email"] is None
    assert row["broker_phone"] is None


def test_broker_email_invalid_returns_none():
    raw = {
        "_links": {"self": {"href": "/cs/v2/estates/1"}},
        "recommendations_data": {},
        "_embedded": {"seller": {"email": ""}},
    }
    assert parse_listing(raw)["broker_email"] is None
    raw["_embedded"]["seller"]["email"] = "not-an-email"
    assert parse_listing(raw)["broker_email"] is None
