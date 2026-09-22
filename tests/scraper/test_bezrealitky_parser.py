"""Hermetic tests for scraper.bezrealitky_parser.

Pure functions over a GraphQL advert dict; no network. The point of these is
that bezrealitky's enum vocabulary maps onto the SAME canonical labels sreality
stores (verified against the live listings table), so cross-source filtering /
dedup / condition scoring see one vocabulary.
"""

from __future__ import annotations

import pytest

from scraper import vocabulary
from scraper.bezrealitky_parser import parse_advert
from scraper.vocabulary import disposition_code


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
    assert listing.price_unit == "za nemovitost"
    assert listing.area_m2 == 83
    assert listing.area_basis == "usable"
    assert listing.usable_area == 83
    assert listing.disposition == "5+kk"
    assert listing.lat == pytest.approx(49.91198895)
    assert listing.lon == pytest.approx(17.363417275899)
    assert listing.locality == "Břidličná"
    # bezrealitky carries the full structured triple (street + house_number + zip).
    assert listing.street == "Jesenická"
    assert listing.house_number == "460"
    assert listing.zip == "793 51"
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
    # R11: balcony OR loggia — loggiaSurface=3 alone is enough.
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
    assert listing.price_unit == "za mesic"


def test_land_headline_is_surface_land_stamped_plot():
    # W17. `surfaceLand` used to reach only estate_area, so 2,654 of 2,667 land rows
    # had a parcel stored and no headline area at all.
    listing = parse_advert(_advert(estateType="POZEMEK", disposition=None,
                                   surface=0, surfaceLand=1450))
    assert listing.category_main == "pozemek"
    assert (listing.area_m2, listing.area_basis) == (1450, "plot")
    assert listing.estate_area == 1450


def test_a_dwelling_never_takes_surface_land_as_its_headline():
    listing = parse_advert(_advert(estateType="DUM", surface=148, surfaceLand=905))
    assert (listing.area_m2, listing.area_basis) == (148, "usable")
    assert listing.estate_area == 905


def test_zero_surface_is_none_sentinel():
    # bezrealitky uses 0 as the "not specified" sentinel for numeric fields.
    listing = parse_advert(_advert(surface=0, etage=0, totalFloors=0))
    assert listing.area_m2 is None
    assert listing.floor is None
    assert listing.total_floors is None


def test_a_boolean_is_never_read_as_a_measurement():
    """`float(True)` is 1.0. bezrealitky's advert carries boolean flags beside its
    measures, so one key that flips from a size to a flag would have written a
    1.0 m² garden — a plausible-looking number, which is the worst kind of wrong."""
    from scraper.bezrealitky_parser import _num

    assert _num(True) is None and _num(False) is None
    listing = parse_advert(_advert(frontGarden=True, surfaceLand=True))
    assert listing.garden_area is None
    assert listing.estate_area is None


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


def test_published_at_from_time_activated_when_present():
    # The anon API returns timeActivated NULL today (so published_at is None on
    # the base advert), but the mapping is wired for the day access appears —
    # the one portal that would carry a real timestamp, not just a day.
    from datetime import timezone

    assert parse_advert(_advert()).published_at is None
    listing = parse_advert(_advert(timeActivated="2024-05-06T10:39:22+02:00"))
    assert listing.published_at is not None
    assert listing.published_at.astimezone(timezone.utc).isoformat() == (
        "2024-05-06T08:39:22+00:00"
    )


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
    assert disposition_code(enum) == expected


def test_ruian_identity_fields_reach_raw():
    # W0 item 0m (location-data program): the detail query now requests
    # ruianId (kod ADM — the national address-point id), addressInput and
    # regionTree; parse_advert stores the advert verbatim, so they must land
    # in raw_json for the W1 claims loader. Pins the raw = dict(advert)
    # contract against a future whitelist regression.
    advert = {
        "id": 989482, "uri": "x", "offerType": "PRODEJ", "estateType": "BYT",
        "gps": {"lat": 50.1, "lng": 14.5},
        "ruianId": 22349995,
        "addressInput": "Poděbradská, Hloubětín, Praha 14, 194 00, Česko",
        "regionTree": [
            {"id": "486", "lvl": 2, "type": "ADMINISTRATIVE",
             "subType": "REGION", "name": "Praha"},
        ],
    }
    listing = parse_advert(advert)
    assert listing.raw["ruianId"] == 22349995
    assert listing.raw["addressInput"].startswith("Poděbradská")
    assert listing.raw["regionTree"][0]["subType"] == "REGION"


def test_a_terrace_alone_is_not_a_balcony():
    """R11: has_balcony is balcony OR loggia, and the terrace has its own column.

    bezrealitky folded `terraceSurface` into the combined flag as well, so 395 of its
    1,417 true rows were terraces — and the same listing's `terrace` said so already."""
    listing = parse_advert(_advert(loggiaSurface=None, terraceSurface=6))
    assert listing.terrace is True
    assert listing.has_balcony is None


def test_has_parking_can_be_unknown_and_a_stated_false_survives():
    """`bool(parking or garage)` could not return None at all, so a JSON null read as a
    stated "no parking" — and the NULL-only text lane could never revise it."""
    assert parse_advert(_advert(parking=None, garage=None)).has_parking is None
    assert parse_advert(_advert(parking=None, garage=False)).has_parking is False
    assert parse_advert(_advert(parking=False, garage=True)).has_parking is True


def test_a_non_czk_price_is_refused_never_converted():
    """price_czk is a CZK total by contract on all nine portals. 31 active rows quote
    the rent in EUR; stored as CZK a 1,124 EUR Prague rent reads as 1,124 CZK."""
    vocabulary.take_unmapped()
    listing = parse_advert(_advert(price=1124, currency="EUR"))
    assert listing.price_czk is None
    assert vocabulary.take_unmapped() == [("price_czk/bezrealitky/eur", 1)]
    assert parse_advert(_advert(price=1124, currency="CZK")).price_czk == 1124
