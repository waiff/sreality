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
# Rule 15's sanctioned cross-types, as PAIRS: dům, komerční and pozemek merge with each other
# (E935, 2026-10-04), a flat with a commercial unit (E938, 2026-10-06); `ostatni` with nothing.
CROSS_TYPES = {frozenset({"dum", "komercni"}), frozenset({"pozemek", "dum"}),
               frozenset({"pozemek", "komercni"}), frozenset({"byt", "komercni"})}


@pytest.mark.parametrize("a", CATEGORIES)
@pytest.mark.parametrize("b", CATEGORIES)
def test_category_main_compatible_matrix(a: str | None, b: str | None) -> None:
    """Equal, or either side unknown, is compatible; of two different known categories only
    the four pairs are, in both directions."""
    expected = a is None or b is None or a == b or frozenset({a, b}) in CROSS_TYPES
    assert rt.category_main_compatible(a, b) is expected
    assert rt.category_main_compatible(b, a) is expected


def test_land_merges_with_a_house_or_commercial_but_never_with_a_flat_or_other() -> None:
    assert rt.category_main_compatible("pozemek", "dum")
    assert rt.category_main_compatible("komercni", "pozemek")
    assert not rt.category_main_compatible("pozemek", "byt")
    assert not rt.category_main_compatible("ostatni", "pozemek")
    assert not rt.category_main_compatible("byt", "dum")


def test_a_flat_merges_with_a_commercial_unit_and_still_never_with_a_house() -> None:
    """E938 (2026-10-06): one studio filed as a flat on one portal and as a commercial unit on
    another. The relation is a set of PAIRS, not an equivalence: komerční meets the flat and
    the house, the flat and the house still never meet, nor the flat and land."""
    assert rt.category_main_compatible("byt", "komercni")
    assert rt.category_main_compatible("komercni", "byt")
    assert rt.category_main_compatible("komercni", "dum")
    assert not rt.category_main_compatible("byt", "dum")
    assert not rt.category_main_compatible("byt", "pozemek")
    assert not rt.category_main_compatible("byt", "ostatni")


def test_the_only_non_transitive_triples_run_from_a_flat_through_a_commercial_unit() -> None:
    known = [c for c in CATEGORIES if c is not None]
    ok = rt.category_main_compatible
    broken = {(a, b, c) for a in known for b in known for c in known
              if len({a, b, c}) == 3 and ok(a, b) and ok(b, c) and not ok(a, c)}
    assert broken == {("byt", "komercni", "dum"), ("dum", "komercni", "byt"),
                      ("byt", "komercni", "pozemek"), ("pozemek", "komercni", "byt")}
