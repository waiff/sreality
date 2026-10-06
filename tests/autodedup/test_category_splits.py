"""The category review's sides (E937): which ads of one property can be one property.

Two ads are on one side when rule 15's gate passes them (`category_clash`, the one definition);
an ad of unknown deal type or category, and a contentless record, ride with the kept side and
never make a property mixed. Ad ids echo the production cases the page was built for.
"""

from __future__ import annotations

import pytest

from autodedup.category_splits import Ad, Side, contentless, rides, sides
from toolkit import room_taxonomy


def ad(listing_id: int, deal: str | None, main: str | None, *, origin: int | None = 1,
       empty: bool = False) -> Ad:
    return Ad(listing_id, deal, main, empty, origin)


def ids(found: list[Side]) -> list[tuple[int, ...]]:
    return [s.ads + s.riders for s in found]


def test_a_sale_and_a_rental_of_one_flat_are_two_sides() -> None:
    """12664: six sale ads and three rental ads of one 30 m² flat."""
    found = sides([*(ad(i, "prodej", "byt") for i in range(1, 7)),
                   *(ad(i, "pronajem", "byt") for i in range(7, 10))], canonical=1)
    assert ids(found) == [(1, 2, 3, 4, 5, 6), (7, 8, 9)]


def test_a_house_and_a_commercial_listing_are_one_side() -> None:
    """53488: two house ads and a commercial ad are one side, the flat ad the other."""
    found = sides([ad(11, "prodej", "dum"), ad(12, "prodej", "dum"), ad(13, "prodej", "byt"),
                   ad(14, "prodej", "komercni")], canonical=11)
    assert ids(found) == [(11, 12, 14), (13,)]


def test_a_flat_and_a_commercial_unit_are_two_sides() -> None:
    """9737: two flat ads and two commercial ads."""
    found = sides([ad(21, "prodej", "komercni"), ad(22, "prodej", "komercni"),
                   ad(23, "prodej", "byt"), ad(24, "prodej", "byt")], canonical=21)
    assert ids(found) == [(21, 22), (23, 24)]


def test_three_sides() -> None:
    found = sides([ad(31, "pronajem", "byt"), ad(32, "prodej", "byt"),
                   ad(33, "prodej", "komercni"), ad(34, "prodej", "byt")], canonical=33)
    assert ids(found) == [(33,), (31,), (32, 34)]


def test_a_contentless_record_never_makes_a_rental_property_mixed() -> None:
    """914: nineteen rental ads and one Bazoš index sighting stored under `byt` / `prodej`."""
    rentals = [ad(i, "pronajem", "byt") for i in range(100, 119)]
    found = sides([*rentals, ad(119, "prodej", "byt", empty=True)], canonical=100)
    assert found == [Side(tuple(range(100, 119)), (119,), True)]


def test_an_ad_of_unknown_category_rides_with_the_kept_side() -> None:
    found = sides([ad(41, "prodej", "byt", origin=None), ad(42, "pronajem", "byt"),
                   ad(43, None, "byt"), ad(44, "prodej", None)], canonical=42)
    assert found == [Side((42,), (), False), Side((41,), (43, 44), True)]


def test_a_share_sale_beside_a_sale_is_two_sides_today() -> None:
    """197654: two house sales and a share sale (`podil`) — any deal-type mismatch clashes."""
    found = sides([ad(51, "prodej", "dum"), ad(52, "prodej", "dum"), ad(53, "podil", "dum")],
                  canonical=51)
    assert ids(found) == [(51, 52), (53,)]


@pytest.mark.parametrize(("pairs", "expected"), [
    (frozenset(), [(61,), (62,)]),
    (frozenset({frozenset({"dum", "komercni"}), frozenset({"byt", "komercni"})}), [(61, 62, 63)]),
])
def test_the_sides_follow_the_one_definition(monkeypatch: pytest.MonkeyPatch, pairs, expected):
    """No category pair is spelled here: change the sanctioned cross-types and the sides move."""
    monkeypatch.setattr(room_taxonomy, "_CROSS_TYPE_OK", pairs)
    adverts = [ad(61, "prodej", "dum"), ad(62, "prodej", "komercni")]
    if len(expected) == 1:
        adverts.append(ad(63, "prodej", "byt"))
    assert ids(sides(adverts, canonical=61)) == expected


def test_the_kept_side_holds_most_of_the_property_s_own_ads() -> None:
    """Not the canonical ad's side when a merge brought the canonical ad: the record stays with
    the property's own ads (E919), as the property page's letters choose."""
    found = sides([ad(71, "prodej", "byt", origin=900), ad(72, "pronajem", "byt", origin=None),
                   ad(73, "pronajem", "byt", origin=None), ad(74, "prodej", "byt", origin=None)],
                  canonical=71)
    assert [(s.ads, s.kept) for s in found] == [((71, 74), False), ((72, 73), True)]


def test_a_tie_keeps_the_earliest_side_and_no_own_ad_keeps_the_canonical_side() -> None:
    tie = sides([ad(81, "pronajem", "byt", origin=None), ad(82, "prodej", "byt", origin=None)],
                canonical=82)
    assert [(s.ads, s.kept) for s in tie] == [((82,), True), ((81,), False)]
    merged = sides([ad(83, "pronajem", "byt"), ad(84, "prodej", "byt")], canonical=84)
    assert [(s.ads, s.kept) for s in merged] == [((84,), True), ((83,), False)]


def test_a_contentless_canonical_ad_brings_its_side_first() -> None:
    found = sides([ad(91, "pronajem", "byt", origin=None), ad(92, "prodej", "byt"),
                   ad(93, "prodej", "byt", empty=True)], canonical=93)
    assert found == [Side((91,), (93,), True), Side((92,), (), False)]


def test_a_property_with_no_known_ad_is_one_side_of_riders() -> None:
    assert sides([ad(1, None, None), ad(2, "prodej", "byt", empty=True)], canonical=1) == [
        Side((), (1, 2), True)]
    assert sides([], canonical=None) == []


@pytest.mark.parametrize(("price", "area", "disposition", "description", "expected"), [
    (None, None, None, None, True),
    (None, None, None, "  \n ", True),
    (2_990_000, None, None, None, False),
    (None, 30, None, None, False),
    (None, None, "1+kk", None, False),
    (None, None, None, "Pronájem bytu 1+kk", False),
])
def test_a_contentless_record(price, area, disposition, description, expected) -> None:
    assert contentless(price, area, disposition, description) is expected


def test_what_rides() -> None:
    assert rides(ad(1, "prodej", "byt", empty=True))
    assert rides(ad(1, None, "byt")) and rides(ad(1, "prodej", None))
    assert not rides(ad(1, "prodej", "byt"))
