"""The single-source image-tag taxonomy + family grouping is internally consistent."""

from __future__ import annotations

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


def test_category_main_compatible() -> None:
    # equal, or either side unknown, is compatible
    assert rt.category_main_compatible("byt", "byt")
    assert rt.category_main_compatible(None, "byt")
    assert rt.category_main_compatible("byt", None)
    # the ONE sanctioned cross-type, both directions
    assert rt.category_main_compatible("dum", "komercni")
    assert rt.category_main_compatible("komercni", "dum")
    # everything else is a hard reject
    assert not rt.category_main_compatible("byt", "dum")
    assert not rt.category_main_compatible("byt", "pozemek")
    assert not rt.category_main_compatible("pozemek", "dum")
