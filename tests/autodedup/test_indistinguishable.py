"""D43's predicate: every fact it can read, and every way it must abstain."""

from __future__ import annotations

import pytest

from autodedup.dataset import Listing, Location
from autodedup.indistinguishable import (
    FACT_NAMES,
    distinguishing_facts,
    indistinguishable,
)


def listing(listing_id: int, **kwargs: object) -> Listing:
    fields: dict[str, object] = {
        "source": "sreality",
        "category_main": "byt",
        "category_type": "prodej",
        "disposition": "2+kk",
        "area_m2": 70.0,
        "floor": 3,
        "price": 5_000_000.0,
    }
    fields.update(kwargs)
    return Listing(id=listing_id, block="b", **fields)  # type: ignore[arg-type]


def names(a: Listing, b: Listing, feats: object = None) -> list[str]:
    return [fact.name for fact in distinguishing_facts(a, b, feats)]  # type: ignore[arg-type]


def test_identical_adverts_are_indistinguishable() -> None:
    assert indistinguishable(listing(1), listing(2))
    assert names(listing(1), listing(2)) == []


def test_predicate_is_symmetric() -> None:
    a, b = listing(1, area_m2=70.0), listing(2, area_m2=80.0, source="bazos")
    assert names(a, b) == names(b, a)


# --- area ----------------------------------------------------------------------------------


def test_one_tape_measure_read_twice_is_not_a_difference() -> None:
    """70 vs 72 m² across two portals is one flat (W7, pair 469862 x 15290511)."""
    a = listing(1, area_m2=70.0)
    b = listing(2, area_m2=72.0, source="bazos")
    assert "area" not in names(a, b)


def test_area_beyond_the_band_is_a_difference() -> None:
    assert "area" in names(listing(1, area_m2=70.0), listing(2, area_m2=80.0))


def test_area_missing_on_one_side_is_not_a_difference() -> None:
    assert "area" not in names(listing(1, area_m2=None), listing(2, area_m2=80.0))


def test_stated_areas_that_cannot_be_the_same_unit() -> None:
    a = listing(1, area_m2=None, description="Prodej bytu o vymere 47 m2 v centru.")
    b = listing(2, area_m2=None, description="Nabizime byt 95 m2 v centru.")
    assert "stated_area" in names(a, b)


def test_stated_areas_within_the_printing_tolerance_agree() -> None:
    a = listing(1, area_m2=None, description="Byt o vymere 47,6 m2.")
    b = listing(2, area_m2=None, description="Byt o vymere 46,6 m2.")
    assert "stated_area" not in names(a, b)


# --- disposition, floors -------------------------------------------------------------------


def test_disposition_difference() -> None:
    assert "disposition" in names(listing(1), listing(2, disposition="3+kk"))


def test_land_has_no_disposition_fact() -> None:
    a = listing(1, category_main="pozemek", disposition="2+kk", floor=None)
    b = listing(2, category_main="pozemek", disposition="3+kk", floor=None)
    assert "disposition" not in names(a, b)


def test_two_floors_apart_is_a_difference_on_either_portal() -> None:
    assert "floor" in names(listing(1, floor=3), listing(2, floor=5, source="bazos"))


def test_one_floor_apart_counts_only_within_one_portal() -> None:
    same = names(listing(1, floor=3), listing(2, floor=4))
    cross = names(listing(1, floor=3), listing(2, floor=4, source="bazos"))
    assert "floor" in same
    assert "floor" not in cross


def test_total_floors_difference() -> None:
    a = listing(1, total_floors=8)
    b = listing(2, total_floors=6)
    assert "total_floors" in names(a, b)
    assert "total_floors" not in names(listing(1, total_floors=None), b)


# --- plot ----------------------------------------------------------------------------------


def test_plot_area_difference_beyond_two_percent() -> None:
    a = listing(1, category_main="dum", attrs={"estate_area": 1000.0})
    b = listing(2, category_main="dum", attrs={"estate_area": 1400.0})
    assert "plot_area" in names(a, b)


def test_plot_area_within_two_percent_agrees() -> None:
    a = listing(1, category_main="dum", attrs={"estate_area": 1000.0})
    b = listing(2, category_main="dum", attrs={"estate_area": 1010.0})
    assert "plot_area" not in names(a, b)


# --- price ---------------------------------------------------------------------------------


def test_a_modest_price_gap_counts_across_portals_but_not_within_one() -> None:
    cross = names(listing(1, price=5_000_000.0),
                  listing(2, price=6_000_000.0, source="bazos"))
    same = names(listing(1, price=5_000_000.0), listing(2, price=6_000_000.0))
    assert "price" in cross
    assert "price" not in same


def test_a_gross_price_gap_counts_within_one_portal_too() -> None:
    """9.9M against 2.5M on bazos is not one asking price at two moments."""
    assert "price" in names(listing(1, price=9_900_000.0), listing(2, price=2_495_000.0))


def test_a_small_cross_portal_price_gap_is_not_a_difference() -> None:
    a = listing(1, price=5_000_000.0)
    b = listing(2, price=5_100_000.0, source="bazos")
    assert "price" not in names(a, b)


def test_missing_price_on_one_side_is_not_a_difference() -> None:
    a = listing(1, price=None)
    b = listing(2, price=6_000_000.0, source="bazos")
    assert "price" not in names(a, b)


# --- where the advert says the unit is ------------------------------------------------------


def _at(listing_id: int, obec: int | None = None, street: str | None = None,
        ruian: int | None = None, cp: str | None = None, **kwargs: object) -> Listing:
    row = listing(listing_id, **kwargs)
    row.location = Location(obec_kod=obec, street_key=street, ruian_adm_kod=ruian,
                            house_number=cp)
    return row


def test_two_municipalities_are_two_units() -> None:
    assert "obec" in names(_at(1, obec=554782), _at(2, obec=563510))


def test_two_streets_are_two_units() -> None:
    assert "street" in names(_at(1, obec=554782, street="jandova"),
                             _at(2, obec=554782, street="nepilova"))


def test_one_street_known_on_one_side_only_says_nothing() -> None:
    assert names(_at(1, obec=554782, street="jandova"), _at(2, obec=554782)) == []


def test_two_entrances_of_one_street_are_not_a_difference() -> None:
    """A RÚIAN code and a house number differ on 4.2 % / 3.7 % of known duplicates."""
    a = _at(1, obec=554782, street="k botici", ruian=22672176, cp="1453/6")
    b = _at(2, obec=554782, street="k botici", ruian=99999999, cp="1455/8")
    assert names(a, b) == []


# --- the orientation the body prints --------------------------------------------------------


def test_two_orientations_are_two_flats() -> None:
    a = listing(1, description="Orientace obou pokoju je na jihovychod, okna s trojskly.")
    b = listing(2, description="Orientace je na vychod, okna s trojskly.")
    assert "orientation" in names(a, b)


def test_a_through_flat_naming_two_directions_abstains() -> None:
    a = listing(1, description="Diky orientaci na jih i na sever byt prirozene vetra.")
    b = listing(2, description="Orientace je na vychod.")
    assert "orientation" not in names(a, b)


def test_an_abbreviated_orientation_is_not_read() -> None:
    a = listing(1, description="Novy byt 2+kk, orientovany na J/Z, vybaveny kuchynskou linkou.")
    b = listing(2, description="Byt s vyhledem orientovanym na jihozapad.")
    assert "orientation" not in names(a, b)


def test_the_same_orientation_is_not_a_difference() -> None:
    a = listing(1, description="Byt je orientovany na jihozapad.")
    b = listing(2, description="Orientace bytu je na jihozapad.")
    assert "orientation" not in names(a, b)


# --- unit designator (E61) -----------------------------------------------------------------


def _at_block(listing_id: int, text: str) -> Listing:
    row = listing(listing_id, description=text)
    row.location = Location(ruian_adm_kod=21778370)
    return row


def test_two_unit_numbers_at_one_address_block() -> None:
    a = _at_block(1, "Prodej bytu (c.3) v novostavbe.")
    b = _at_block(2, "Prodej bytu (c.5) v novostavbe.")
    assert "unit_designator" in names(a, b)


def test_unit_numbers_in_different_blocks_do_not_conflict() -> None:
    a = _at_block(1, "Prodej bytu (c.3).")
    b = listing(2, description="Prodej bytu (c.5).")
    b.location = Location(ruian_adm_kod=99999999)
    assert "unit_designator" not in names(a, b)


def test_a_price_list_of_several_units_conflicts_with_nothing() -> None:
    a = _at_block(1, "K dispozici jednotky c. 3, jednotky c. 5 a jednotky c. 7.")
    b = _at_block(2, "Prodej bytu (c.9).")
    assert "unit_designator" not in names(a, b)


# --- image facts, which come only from the engine's own feature row --------------------------


def _feats(**kwargs: float) -> dict[str, tuple[float, bool]]:
    return {name: (value, True) for name, value in kwargs.items()}


def test_no_feature_row_means_no_image_fact() -> None:
    assert names(listing(1), listing(2), None) == []


def test_a_floorplan_conflict_alone_is_refused() -> None:
    feats = _feats(floorplan_conflict=1.0, tag_room_clip_min2=0.99)
    assert "floorplan" not in names(listing(1), listing(2), feats)


def test_a_floorplan_conflict_with_a_weak_room_match_counts() -> None:
    feats = _feats(floorplan_conflict=1.0, tag_room_clip_min2=0.80)
    assert "floorplan" in names(listing(1), listing(2), feats)


def test_a_weak_room_match_is_an_interior_difference() -> None:
    feats = _feats(tag_room_clip_min2=0.80)
    assert "interior" in names(listing(1), listing(2), feats)


def test_a_strong_room_match_is_not() -> None:
    feats = _feats(tag_room_clip_min2=0.99)
    assert "interior" not in names(listing(1), listing(2), feats)


def test_an_absent_feature_slot_is_not_a_difference() -> None:
    feats = {"tag_room_clip_min2": (0.0, False), "floorplan_conflict": (1.0, False)}
    assert names(listing(1), listing(2), feats) == []


# --- the standing rulings -------------------------------------------------------------------


def test_deal_type_and_category_are_always_differences() -> None:
    a = listing(1, category_type="prodej")
    b = listing(2, category_type="pronajem")
    assert "category_type" in names(a, b)
    assert "category_main" in names(listing(1, category_main="byt"),
                                    listing(2, category_main="komercni"))


def test_the_sanctioned_cross_type_is_not_a_difference() -> None:
    """dům <-> komerční is rule #15's one sanctioned cross-type; the operator confirmed 8."""
    a = listing(1, category_main="dum", disposition=None, floor=None)
    b = listing(2, category_main="komercni", disposition=None, floor=None)
    assert "category_main" not in names(a, b)


# --- contract ---------------------------------------------------------------------------------


def test_every_returned_name_is_declared() -> None:
    a = listing(1, area_m2=70.0, floor=3, total_floors=8, price=5_000_000.0,
                category_type="prodej", category_main="byt", disposition="2+kk",
                description="Byt (c.3) o vymere 47 m2.", attrs={"estate_area": 1000.0})
    a.location = Location(ruian_adm_kod=21778370)
    b = listing(2, area_m2=95.0, floor=7, total_floors=6, price=9_000_000.0, source="bazos",
                category_type="pronajem", category_main="komercni", disposition="3+kk",
                description="Byt (c.9) o vymere 95 m2.", attrs={"estate_area": 2000.0})
    b.location = Location(ruian_adm_kod=21778370)
    feats = _feats(floorplan_conflict=1.0, tag_room_clip_min2=0.5)
    found = names(a, b, feats)
    assert set(found) <= set(FACT_NAMES)
    assert len(found) == len(set(found))
    assert not indistinguishable(a, b, feats)


@pytest.mark.parametrize("name", FACT_NAMES)
def test_fact_names_are_stable_strings(name: str) -> None:
    assert name and name.islower()
