"""Hermetic tests for scraper.bezrealitky_parser.

Pure functions over a GraphQL advert dict; no network. The point of these is
that bezrealitky's enum vocabulary maps onto the SAME canonical labels sreality
stores (verified against the live listings table), so cross-source filtering /
dedup / condition scoring see one vocabulary.
"""

from __future__ import annotations

import pytest

from scraper.bezrealitky_parser import _disposition, parse_advert


def _advert(**over):
    base = {
        "id": "1024119",
        "uri": "1024119-nabidka-prodej-bytu-jesenicka-bridlicna",
        "title": "Prodej bytu 5+kk 83 m²",
        "description": "  Hezký byt.  ",
        "offerType": "PRODEJ",
        "estateType": "BYT",
        "disposition": "DISP_5_KK",
        "price": 3290000,
        "currency": "CZK",
        "charges": 0,
        "originalPrice": 0,
        "isDiscounted": False,
        "surface": 83,
        "surfaceLand": None,
        "frontGarden": None,
        "balconySurface": None,
        "terraceSurface": None,
        "cellarSurface": 4,
        "loggiaSurface": 3,
        "gps": {"lat": 49.91198895, "lng": 17.363417275899},
        "address": "Jesenická, Břidličná",
        "street": "Jesenická",
        "houseNumber": "460",
        "city": "Břidličná",
        "cityDistrict": "Břidličná",
        "zip": "793 51",
        "construction": "PANEL",
        "condition": "AFTER_RECONSTRUCTION",
        "ownership": "OSOBNI",
        "equipped": "NEVYBAVENY",
        "penb": "C",
        "etage": 5,
        "totalFloors": 8,
        "parking": True,
        "garage": False,
        "lift": True,
        "active": True,
        "mainImage": {"url": "https://api.bezrealitky.cz/m/a.jpg"},
        "publicImages": [
            {"url": "https://api.bezrealitky.cz/m/b.jpg", "order": 2},
            {"url": "https://api.bezrealitky.cz/m/a.jpg", "order": 1},
        ],
    }
    base.update(over)
    return base


def test_core_mapping():
    listing = parse_advert(_advert())
    assert listing.source == "bezrealitky"
    assert listing.source_id_native == "1024119"
    assert listing.source_url.endswith(
        "/nemovitosti-byty-domy/1024119-nabidka-prodej-bytu-jesenicka-bridlicna"
    )
    assert listing.category_main == "byt"
    assert listing.category_type == "prodej"
    assert listing.price_czk == 3290000
    assert listing.price_unit == "celkem"
    assert listing.area_m2 == 83
    assert listing.usable_area == 83
    assert listing.disposition == "5+kk"
    assert listing.lat == pytest.approx(49.91198895)
    assert listing.lon == pytest.approx(17.363417275899)
    assert listing.locality == "Břidličná"
    assert listing.building_type == "panel"
    assert listing.condition == "po_rekonstrukci"
    assert listing.ownership == "osobni"
    assert listing.furnished == "ne"
    assert listing.energy_rating == "C"
    assert listing.floor == 5
    assert listing.total_floors == 8
    assert listing.has_lift is True
    assert listing.description == "Hezký byt."
    assert listing.subtype is None  # BYT has no property sub-type


def test_office_estate_type_maps_to_subtype():
    listing = parse_advert(_advert(estateType="KANCELAR"))
    assert listing.category_main == "komercni"
    assert listing.subtype == "kancelar"


def test_generic_commercial_has_no_subtype():
    listing = parse_advert(_advert(estateType="NEBYTOVY_PROSTOR"))
    assert listing.category_main == "komercni"
    assert listing.subtype is None


def test_wood_construction_canonicalises_to_drevo():
    # cross-source canonical value is "drevo" (the building_type filter option),
    # not "dřevostavba" — keep bezrealitky aligned with sreality/idnes/maxima.
    listing = parse_advert(_advert(construction="WOOD"))
    assert listing.building_type == "drevo"


def test_surface_derived_flags():
    listing = parse_advert(_advert())
    # loggiaSurface=3 -> the legacy combined balcony flag is true
    assert listing.has_balcony is True
    # cellarSurface=4 -> cellar true; terraceSurface null -> terrace unknown
    assert listing.cellar is True
    assert listing.terrace is None
    # parking bool true -> legacy has_parking true; no count available
    assert listing.has_parking is True
    assert listing.parking_lots is None
    assert listing.garage is False


def test_image_urls_ordered_in_raw():
    listing = parse_advert(_advert())
    assert listing.raw["image_urls"] == [
        "https://api.bezrealitky.cz/m/a.jpg",  # order 1 first
        "https://api.bezrealitky.cz/m/b.jpg",  # order 2
    ]


def test_rent_price_unit():
    listing = parse_advert(_advert(offerType="PRONAJEM", estateType="BYT"))
    assert listing.category_type == "pronajem"
    assert listing.price_unit == "měsíc"


def test_zero_surface_is_none_sentinel():
    # bezrealitky uses 0 as the "not specified" sentinel for numeric fields.
    listing = parse_advert(_advert(surface=0, etage=0, totalFloors=0))
    assert listing.area_m2 is None
    assert listing.floor is None
    assert listing.total_floors is None


def test_missing_gps_yields_none_coords():
    listing = parse_advert(_advert(gps=None))
    assert listing.lat is None and listing.lon is None


def test_unmapped_enums_become_none():
    listing = parse_advert(_advert(
        construction="UNDEFINED", condition="UNDEFINED",
        ownership="OSTATNI", equipped="UNDEFINED", penb="UNDEFINED",
        disposition="OSTATNI",
    ))
    assert listing.building_type is None
    assert listing.condition is None
    assert listing.ownership is None
    assert listing.furnished is None
    assert listing.energy_rating is None
    assert listing.disposition is None


def test_commercial_and_land_category_mapping():
    assert parse_advert(_advert(estateType="KANCELAR")).category_main == "komercni"
    assert parse_advert(_advert(estateType="NEBYTOVY_PROSTOR")).category_main == "komercni"
    assert parse_advert(_advert(estateType="POZEMEK")).category_main == "pozemek"
    assert parse_advert(_advert(estateType="GARAZ")).category_main == "ostatni"


@pytest.mark.parametrize("enum,expected", [
    ("DISP_1_KK", "1+kk"),
    ("DISP_2_1", "2+1"),
    ("DISP_3_KK", "3+kk"),
    ("GARSONIERA", "1+kk"),
    ("DISP_4_IZB", "4+1"),
    ("OSTATNI", None),
    ("UNDEFINED", None),
    (None, None),
])
def test_disposition_mapping(enum, expected):
    assert _disposition(enum) == expected
