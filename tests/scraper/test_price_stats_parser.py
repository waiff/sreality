"""Hermetic tests for scraper.price_stats_parser (params + JSON folding)."""

from __future__ import annotations

import pytest

from scraper.price_stats_parser import (
    PriceStatsParseError,
    build_estate_prices_params,
    parse_estate_prices,
    parse_suggest_municipality,
)


def test_params_includes_only_set_filters():
    ds = {"category_main_cb": 1, "distance": 0}  # no condition/type/ownership/area
    params = build_estate_prices_params(
        ds, entity_id=96, entity_type="muni",
        category_type_cb=2, default_from="2015-01", default_to="2026-06",
    )
    assert params["category_type_cb"] == 2
    assert params["entity_type"] == "muni"
    assert params["default_from"] == "2015-01"
    for omitted in ("building_condition", "building_type", "ownership",
                    "usable_area_from", "usable_area_to"):
        assert omitted not in params


def test_params_codes_are_strings_areas_ints():
    ds = {
        "category_main_cb": 1, "building_condition": "1", "building_type": "5",
        "ownership": "1", "usable_area_from": 30, "usable_area_to": 80, "distance": 0,
    }
    params = build_estate_prices_params(
        ds, entity_id=3412, entity_type="muni",
        category_type_cb=1, default_from="2015-01", default_to="2026-06",
    )
    assert params["building_condition"] == "1"
    assert params["building_type"] == "5"
    assert params["usable_area_from"] == 30
    assert isinstance(params["usable_area_from"], int)


def test_parse_estate_prices_merges_price_and_counts():
    payload = {"result": {
        "advert_count": 12,
        "avg_price_per_area": 50000,
        "dev_price_by_month": [
            {"year": 2024, "month": 11, "price": 48000},
            {"year": 2024, "month": 12, "price": 49000},
        ],
        "dev_count_advert_by_month": [
            {"year": 2024, "month": 12, "active": 12, "new": 3, "deleted": 1},
        ],
    }}
    out = parse_estate_prices(payload)
    assert [(m["year"], m["month"]) for m in out["months"]] == [(2024, 11), (2024, 12)]
    nov, dec = out["months"]
    assert nov["price"] == 48000 and nov["active_count"] is None  # price-only month
    assert dec["price"] == 49000 and dec["active_count"] == 12
    assert out["aggregates"]["advert_count"] == 12


def test_parse_estate_prices_bad_shape_raises():
    with pytest.raises(PriceStatsParseError):
        parse_estate_prices({"nope": 1})


def test_parse_suggest_prefers_exact_municipality():
    payload = {"results": [
        {"userData": {"source": "stre", "id": 99, "municipality": "Kolín"}},
        {"userData": {"source": "muni", "id": 3412, "municipality": "Kolín",
                      "district": "Kolín", "region_id": 11,
                      "latitude": 50.02, "longitude": 15.2}},
        {"userData": {"source": "muni", "id": 5, "municipality": "Kolínec"}},
    ]}
    match = parse_suggest_municipality(payload, phrase="Kolín")
    assert match["entity_id"] == 3412
    assert match["entity_type"] == "muni"
    assert match["lat"] == pytest.approx(50.02)


def test_parse_suggest_no_municipality_returns_none():
    payload = {"results": [{"userData": {"source": "stre", "id": 1}}]}
    assert parse_suggest_municipality(payload, phrase="x") is None
