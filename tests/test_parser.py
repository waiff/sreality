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


def test_floor(sample):
    assert parse_listing(sample)["floor"] == 5


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
