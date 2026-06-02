"""Sanity tests for scraper.price_stats_enums code maps."""

from __future__ import annotations

from scraper import price_stats_enums as e


def test_building_type_uses_stats_map_not_search_facet():
    # The stats page maps Panel=5 (the search facet uses Panel=1); the HAR
    # request confirmed building_type=5 for konstrukce=panel.
    assert e.BUILDING_TYPE["5"] == "panel"
    assert e.BUILDING_TYPE["2"] == "cihla"


def test_label_for_known_and_unknown():
    assert e.label_for("building_condition", "1") == "Velmi dobrý"
    assert e.label_for("ownership", "1") == "Osobní"
    assert e.label_for("building_condition", "999") is None
    assert e.label_for("building_condition", None) is None


def test_category_type_constants():
    assert e.SALE == 1 and e.LEASE == 2
    assert e.CATEGORY_TYPE[1] == "prodej" and e.CATEGORY_TYPE[2] == "pronajem"


def test_full_condition_enum_present():
    # All 10 sreality "Stav objektu" codes mapped (API accepts any).
    assert set(e.BUILDING_CONDITION) == {str(i) for i in range(1, 11)}
