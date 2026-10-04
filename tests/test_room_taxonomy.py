"""The single-source image-tag taxonomy + family grouping is internally consistent."""

from __future__ import annotations

import pytest

from toolkit import room_taxonomy as rt


def test_every_tag_has_a_family() -> None:
    assert set(rt.ROOM_TYPES) == set(rt.ROOM_FAMILIES)
    assert all(fam in ("interior", "exterior", "common", "plan", "other")
               for fam in rt.ROOM_FAMILIES.values())


def test_plan_constants_are_taxonomy_tags() -> None:
    assert rt.ROOM_FAMILIES[rt.SITE_PLAN_ROOM_TYPE] == "plan"
    assert rt.ROOM_FAMILIES[rt.FLOOR_PLAN_ROOM_TYPE] == "plan"


def test_family_of() -> None:
    assert rt.family_of("kitchen") == "interior"
    assert rt.family_of("exterior_facade") == "exterior"
    assert rt.family_of("staircase_interior") == "common"
    assert rt.family_of("site_plan") == "plan"
    assert rt.family_of("nonexistent_tag") is None
    assert rt.family_of(None) is None


CATEGORIES = ("byt", "dum", "komercni", "pozemek", "ostatni", None)
# Rule 15's sanctioned cross-types (E935, 2026-10-04): dům, komerční and pozemek merge with each
# other; a flat and `ostatni` with no other category.
CROSS_TYPES = {frozenset({"dum", "komercni"}), frozenset({"pozemek", "dum"}),
               frozenset({"pozemek", "komercni"})}


@pytest.mark.parametrize("a", CATEGORIES)
@pytest.mark.parametrize("b", CATEGORIES)
def test_category_main_compatible_matrix(a: str | None, b: str | None) -> None:
    """Equal, or either side unknown, is compatible; of two different known categories only
    the three cross-types are, in both directions."""
    expected = a is None or b is None or a == b or frozenset({a, b}) in CROSS_TYPES
    assert rt.category_main_compatible(a, b) is expected
    assert rt.category_main_compatible(b, a) is expected


def test_land_merges_with_a_house_or_commercial_but_never_with_a_flat_or_other() -> None:
    assert rt.category_main_compatible("pozemek", "dum")
    assert rt.category_main_compatible("komercni", "pozemek")
    assert not rt.category_main_compatible("pozemek", "byt")
    assert not rt.category_main_compatible("ostatni", "pozemek")
    assert not rt.category_main_compatible("byt", "dum")
