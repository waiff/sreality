"""Tests for parser.py against a real-shape Sreality v1 detail fixture.

The live /api/v1/estates detail payload is flat snake_case (id key
`hash_id`, typed attributes at the top level, `{name, value}` enum
objects). The fixture is a real anonymized byt/prodej listing.
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from scraper.parser import SUBTYPE, parse_images, parse_listing

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


def test_subtype_map_values():
    # disjoint house / commercial code ranges, no overlap with apartment codes
    assert SUBTYPE[37] == "rodinny_dum"
    assert SUBTYPE[39] == "vila"
    assert SUBTYPE[25] == "kancelar"
    assert SUBTYPE[26] == "sklad"
    assert 6 not in SUBTYPE  # apartment disposition code, deliberately excluded


def test_subtype_house():
    row = parse_listing(_estate(
        category_main_cb={"value": 2}, category_sub_cb={"name": "Rodinný", "value": 37}))
    assert row["category_main"] == "dum"
    assert row["subtype"] == "rodinny_dum"


def test_subtype_commercial():
    row = parse_listing(_estate(
        category_main_cb={"value": 4}, category_sub_cb={"name": "Kanceláře", "value": 25}))
    assert row["category_main"] == "komercni"
    assert row["subtype"] == "kancelar"


def test_subtype_apartment_is_none():
    # an apartment disposition sub-code is not a property subtype
    row = parse_listing(_estate(
        category_main_cb={"value": 1}, category_sub_cb={"name": "3+kk", "value": 6}))
    assert row["category_main"] == "byt"
    assert row["subtype"] is None


def test_subtype_missing_is_none():
    assert parse_listing(_estate())["subtype"] is None


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
    row = parse_listing(sample)
    assert row["area_m2"] == 85.0
    # sreality's only interior measure is `usable_area`, so the stamp is fixed.
    assert row["area_basis"] == "usable"


def test_area_zero_is_a_placeholder_not_a_measure(sample):
    # sreality's v1 feed renders an absent interior measure as `usable_area: 0`
    # rather than omitting the key. The old code stored that 0.0; the shared
    # resolver treats it as the form placeholder it is. No live row carries
    # area_m2 = 0 today, and sreality's content hash is over raw_json (not the
    # parsed row), so this churns nothing — but the semantics are pinned here.
    row = parse_listing({**sample, "usable_area": 0})
    assert row["area_m2"] is None
    assert row["area_basis"] is None


def test_disposition(sample):
    assert parse_listing(sample)["disposition"] == "3+kk"


def test_disposition_falls_back_to_name():
    row = parse_listing(_estate(category_sub_cb={"name": "atypický", "value": 12},
                                advert_name="Prodej bytu 3+1 100 m²"))
    assert row["disposition"] == "3+1"


def test_the_row_carries_no_place_at_all(sample):
    """W4-c: `listings` has no location columns, so the sreality row dict emits
    none. The payload's own locality block still reaches the DB verbatim inside
    raw_json, which is what the claims lane mines and what the content hash
    covers — a place is a CLAIM the resolver arbitrates into listing_location,
    never a column the parser writes."""
    row = parse_listing(sample)
    for key in ("lat", "lon", "geom", "locality", "district", "street",
                "house_number", "zip", "street_id", "locality_district_id",
                "locality_region_id", "locality_municipality_id",
                "locality_quarter_id", "locality_ward_id"):
        assert key not in row, f"parse_listing still emits {key!r}"
    assert sample["locality"]["gps_lat"] is not None


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


def test_building_type_wood_canonicalises_to_drevo():
    # sreality says "Dřevostavba"; the filter option is "drevo".
    row = parse_listing(_estate(building_type={"name": "Dřevostavba", "value": 1}))
    assert row["building_type"] == "drevo"


def test_building_type_unmapped_is_diacritic_free():
    row = parse_listing(_estate(building_type={"name": "Modulární", "value": 1}))
    assert row["building_type"] == "modularni"


def test_building_type_status_overlay_is_none():
    # Reserved/sold listings overlay the status onto the param name.
    for status in ("Rezervováno", "Prodáno"):
        row = parse_listing(_estate(building_type={"name": status, "value": 1}))
        assert row["building_type"] is None, status


def test_condition(sample):
    # Diacritic-free + underscore-joined, matching the schema convention.
    assert parse_listing(sample)["condition"] == "velmi_dobry"


def test_condition_unspecified_is_none():
    row = parse_listing(_estate(building_condition={"name": "- vyber stav", "value": 0}))
    assert row["condition"] is None


def test_condition_status_overlay_is_none():
    for status in ("Rezervováno", "Prodáno"):
        row = parse_listing(_estate(building_condition={"name": status, "value": 1}))
        assert row["condition"] is None, status


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


def test_parse_images_keeps_bare_path():
    """The CDN render-transform is a download-time concern, not identity: the
    parser stores the bare path so the stored URL stays canonical."""
    raw = _estate(advert_images=[{"url": "//d18-a.sdn.cz/x/y.jpeg", "order": 1}])
    assert "fl=" not in parse_images(raw)[0]["url"]


def test_description(sample):
    assert parse_listing(sample)["description"].startswith("Prodej samostatné bytové jednotky")


def test_description_strips_whitespace():
    assert parse_listing(_estate(advert_description="  Trimmed.\n\n"))["description"] == "Trimmed."


def test_description_missing_is_none():
    assert parse_listing(_estate())["description"] is None


def test_description_empty_value_is_none():
    assert parse_listing(_estate(advert_description="   "))["description"] is None


def test_published_at_from_edited(sample):
    # `edited` is sreality's day-granular last-edit date (~40% of rows) — the
    # weak publish-bound fallback mapped onto published_at.
    assert parse_listing(sample)["published_at"] == date(2026, 5, 26)


def test_published_at_missing_is_none():
    assert parse_listing(_estate())["published_at"] is None


def test_published_at_malformed_is_none():
    assert parse_listing(_estate(edited="not a date"))["published_at"] is None
    assert parse_listing(_estate(edited=20260526))["published_at"] is None


def test_missing_id_raises():
    with pytest.raises(ValueError):
        parse_listing({"locality": {}})
